"""Hermetic CI evidence reads: real traversal and CLI, fake network/clock."""

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import ci_log
import pytest

SHA = "a" * 40
INVOCATION = "4154555a-1339-41a7-a441-f02a06156196"
STATUS = {"id": 10, "state": "failure", "invocation": INVOCATION}


def chunk(text, previous="", **extra):
    return {
        "buffer": base64.b64encode(text.encode()).decode(),
        "previousChunkId": previous,
        **extra,
    }


def test_latest_exact_check_and_fixed_destination(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "sha": SHA,
                    "statuses": [
                        {
                            "id": 9,
                            "state": "success",
                            "target_url": "https://attacker.invalid/old",
                        },
                        {
                            "id": 10,
                            "state": "failure",
                            "target_url": f"https://jomcgi.buildbuddy.io/invocation/{INVOCATION}",
                        },
                    ],
                }
            ).encode(),
        )

    monkeypatch.setattr(ci_log.subprocess, "run", run)
    assert ci_log.read_status("owner/repo", SHA) == STATUS
    argv, options = calls[0]
    assert argv[:6] == ["gh", "api", "--hostname", "github.com", "--method", "GET"]
    assert f"repos/owner/repo/commits/{SHA}/status?per_page=100" in argv
    assert options["timeout"] == 10
    assert not options.get("shell")


@pytest.mark.parametrize("change", ["sha", "url", "state", "missing", "malformed"])
def test_ambiguous_or_untrusted_status_fails_closed(monkeypatch, change):
    value = {
        "sha": SHA,
        "statuses": [
            {
                "id": 10,
                "state": "success",
                "target_url": f"https://jomcgi.buildbuddy.io/invocation/{INVOCATION}",
            }
        ],
    }
    if change == "sha":
        value["sha"] = "b" * 40
    elif change == "url":
        value["statuses"][0]["target_url"] += "?redirect=https://attacker.invalid"
    elif change == "state":
        value["statuses"][0]["state"] = "unknown"
    elif change == "missing":
        value["statuses"] = []
    raw = b"not json" if change == "malformed" else json.dumps(value).encode()
    monkeypatch.setattr(
        ci_log.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout=raw),
    )
    with pytest.raises(ci_log.EvidenceError):
        ci_log.read_status("owner/repo", SHA)


def test_rpc_is_credential_free_and_bounded(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, limit):
            assert limit == ci_log.MAX_RESPONSE + 1
            return json.dumps(chunk("assertion\n")).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == 10
            return Response()

    monkeypatch.setenv("BUILDBUDDY_API_KEY", "must-never-be-used")
    monkeypatch.setattr(ci_log.urllib.request, "build_opener", lambda *_args: Opener())
    ci_log.fetch_chunk(INVOCATION, "0001")
    request = calls[0]
    assert request.full_url == ci_log.LOG_ENDPOINT
    assert dict(request.header_items()) == {"Content-type": "application/json"}
    assert json.loads(request.data) == {
        "invocationId": INVOCATION,
        "chunkId": "0001",
        "minLines": 0,
    }
    with pytest.raises(ci_log.EvidenceError, match="redirected"):
        ci_log.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://attacker.invalid"
        )


def test_multichunk_failure_context_survives_a_long_success_looking_tail():
    pages = {
        "": chunk(
            "ordinary output\n" * 150 + "Executed 0 out of 50 tests\n",
            "0001",
            nextChunkId="0003",
        ),
        "0001": chunk(
            "Traceback (most recent call last):\n  assert expected == observed\nAssertionError: exact mismatch\n",
            "0000",
        ),
        "0000": chunk("setup context\n"),
    }
    calls = []

    def fetch(_invocation, cursor):
        calls.append(cursor)
        return pages[cursor]

    output, state = ci_log.inspect(
        "owner/repo", SHA, status_reader=lambda *_: STATUS, fetch=fetch
    )
    assert calls == ["", "0001", "0000"]
    assert state == "failure" and "pr-checks terminal: failure" in output
    assert "setup context\nTraceback" in output
    assert "assert expected == observed\nAssertionError: exact mismatch" in output
    assert "Executed 0 out of 50 tests" in output
    assert "log lines omitted" in output
    assert output.index("AssertionError") < output.index("Executed 0 out of 50")


def test_stored_chunk_does_not_claim_running_invocation_is_terminal(tmp_path):
    pending = {**STATUS, "state": "pending"}
    output, state = ci_log.inspect(
        "owner/repo",
        SHA,
        cache=tmp_path,
        status_reader=lambda *_: pending,
        fetch=lambda *_: chunk("still working\n", nextChunkId="000b"),
    )
    assert state == "pending" and "pr-checks running: pending" in output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("new_invocation", [INVOCATION, "b" * 8 + INVOCATION[8:]])
def test_terminal_cache_rechecks_status_and_does_not_hide_rerun(
    tmp_path, new_invocation
):
    states = iter([STATUS, STATUS, {**STATUS, "id": 11, "invocation": new_invocation}])
    reads = []

    def fetch(invocation, _cursor):
        reads.append(invocation)
        return chunk("AssertionError: evidence\nBUILDBUDDY_API_KEY=secret-value\n")

    outputs = [
        ci_log.inspect(
            "owner/repo",
            SHA,
            cache=tmp_path,
            status_reader=lambda *_: next(states),
            fetch=fetch,
        )[0]
        for _ in range(3)
    ]
    assert reads == [INVOCATION, new_invocation]
    assert "; cached;" in outputs[1]
    assert "; fetched;" in outputs[2]
    assert all("secret-value" not in output for output in outputs)
    assert all("secret-value" not in path.read_text() for path in tmp_path.iterdir())


def test_live_or_empty_terminal_log_is_not_cached(tmp_path):
    for value in (chunk("still changing", live=True), chunk("")):
        ci_log.cached_log(
            "owner/repo",
            SHA,
            STATUS,
            cache=tmp_path,
            fetch=lambda *_, value=value: value,
        )
    assert not list(tmp_path.iterdir())


def test_bound_wait_polls_status_with_backoff_and_fetches_log_once():
    now, sleeps, calls = [0], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def fetch(*_args):
        calls.append("log")
        return chunk("running\n")

    output, state = ci_log.inspect(
        "owner/repo",
        SHA,
        wait=60,
        status_reader=lambda *_: {**STATUS, "state": "pending"},
        fetch=fetch,
        sleep=sleep,
        clock=lambda: now[0],
    )
    assert sleeps == [2, 4, 8, 16, 16]
    assert sum(sleeps) <= 60 and calls == ["log"]
    assert state == "pending" and "running" in output


def test_expired_or_corrupt_cache_refetches(tmp_path, monkeypatch):
    calls = []

    def fetch(*_args):
        calls.append(True)
        return chunk("AssertionError: original evidence\n")

    ci_log.cached_log("owner/repo", SHA, STATUS, cache=tmp_path, fetch=fetch)
    path = next(tmp_path.glob("*.json"))
    path.write_text("invalid cache with private-output")
    value, hit = ci_log.cached_log(
        "owner/repo", SHA, STATUS, cache=tmp_path, fetch=fetch
    )
    assert not hit and "private-output" not in value["text"]
    monkeypatch.setattr(ci_log, "CACHE_TTL", 0)
    _value, hit = ci_log.cached_log(
        "owner/repo", SHA, STATUS, cache=tmp_path, fetch=fetch
    )
    assert not hit and len(calls) == 3


def test_prior_cache_format_with_a_cut_credential_is_not_reused(tmp_path):
    legacy_key = hashlib.sha256(
        json.dumps([1, "owner/repo", SHA, STATUS], sort_keys=True).encode()
    ).hexdigest()
    (tmp_path / (legacy_key + ".json")).write_text(
        json.dumps(
            {
                "text": "sensitive-token-value\n",
                "chunks": 16,
                "bytes": 22,
                "truncated": "chunk_limit",
                "live": False,
            }
        )
    )
    calls = []

    def fetch(*_args):
        calls.append(True)
        return chunk("safe\n")

    value, hit = ci_log.cached_log(
        "owner/repo", SHA, STATUS, cache=tmp_path, fetch=fetch
    )
    assert calls == [True] and not hit
    assert value["text"] == "safe\n"


@pytest.mark.parametrize(
    "value",
    [
        {"buffer": "bad base64!"},
        chunk("data", previous="../../other"),
        chunk("data", live="false"),
    ],
)
def test_malformed_chunk_is_not_evidence(value):
    with pytest.raises(ci_log.EvidenceError, match="Malformed"):
        ci_log.read_log(INVOCATION, lambda *_: value)


def test_cursor_cycle_and_chunk_bound():
    with pytest.raises(ci_log.EvidenceError, match="Repeated"):
        ci_log.read_log(INVOCATION, lambda *_: chunk("data\n", "0001"))
    log = ci_log.read_log(INVOCATION, lambda *_: chunk("data\n", "0001"), max_chunks=1)
    assert log["truncated"] == "chunk_limit" and log["chunks"] == 1


def test_byte_bound_and_cross_chunk_redaction(monkeypatch):
    pages = {
        "": chunk("sensitive-value\n", "0000"),
        "0000": chunk("BUILDBUDDY_API_KEY="),
    }
    log = ci_log.read_log(INVOCATION, lambda _, cursor: pages[cursor])
    assert "sensitive-value" not in log["text"] and "[REDACTED]" in log["text"]
    monkeypatch.setattr(ci_log, "MAX_BYTES", 20)
    log = ci_log.read_log(
        INVOCATION, lambda *_: chunk("TOKEN=" + "x" * 50 + "\nsafe\n")
    )
    assert log["bytes"] <= 20 and log["truncated"] == "byte_limit"
    assert log["text"] == "safe\n"


@pytest.mark.parametrize(
    "suffix", ["sensitive-token-value\nsafe\n", "sensitive-token-value"]
)
def test_chunk_limit_discards_an_unrecognizable_credential_suffix(suffix):
    log = ci_log.read_log(INVOCATION, lambda *_: chunk(suffix, "0000"), max_chunks=1)
    assert log["truncated"] == "chunk_limit"
    assert "sensitive-token-value" not in log["text"]
    assert log["text"] == ("safe\n" if suffix.endswith("safe\n") else "")


@pytest.mark.parametrize("remaining", [0, 3])
def test_byte_limit_discards_a_partial_line_spanning_retained_chunks(
    monkeypatch, remaining
):
    newer = "sensitive-token-value\nsafe\n"
    monkeypatch.setattr(ci_log, "MAX_BYTES", len(newer) + remaining)
    pages = {"": chunk(newer, "0000"), "0000": chunk("BUILDBUDDY_API_KEY=")}
    log = ci_log.read_log(INVOCATION, lambda _, cursor: pages[cursor])
    assert log["truncated"] == "byte_limit"
    assert log["text"] == "safe\n"


def test_chunk_limit_never_caches_the_cut_credential_prefix(tmp_path):
    calls = []

    def fetch(_invocation, _cursor):
        calls.append(True)
        if len(calls) == 16:
            return chunk("sensitive-token-value\nsafe\n", "0000")
        return chunk("later context\n", f"{16 - len(calls):04x}")

    value, _hit = ci_log.cached_log(
        "owner/repo", SHA, STATUS, cache=tmp_path, fetch=fetch
    )
    assert len(calls) == 16 and value["truncated"] == "chunk_limit"
    assert "sensitive-token-value" not in value["text"]
    assert all(
        "sensitive-token-value" not in path.read_text() for path in tmp_path.iterdir()
    )


def test_truncated_private_key_suffix_is_removed_through_its_end_marker():
    suffix = (
        "first-private-line\nsecond-private-line\n-----END PRIVATE KEY-----\nsafe\n"
    )
    log = ci_log.read_log(INVOCATION, lambda *_: chunk(suffix, "0000"), max_chunks=1)
    assert "private-line" not in log["text"]
    assert log["text"].endswith("safe\n")


def test_redaction_happens_before_excerpt_selection():
    raw = "AssertionError: \x1b[31mTOKEN=private-value\x1b[0m\n"
    raw += "Authorization: Bearer abcdefghijklmnopqrst\n"
    raw += "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----\n"
    text = ci_log.redact(raw)
    assert (
        "private" not in text
        and "abcdefghijklmnopqrst" not in text
        and "\x1b" not in text
    )
    assert "AssertionError" in text


def test_output_bound_keeps_tail_when_error_windows_exceed_it():
    text = "".join(f"ERROR {i}: " + "x" * 100 + "\n" for i in range(1500))
    text += "Executed 8 out of 10 tests\n"
    output = ci_log.excerpts(text)
    assert len(output.encode()) < ci_log.MAX_OUTPUT
    assert "ERROR 0:" in output and "Executed 8 out of 10 tests" in output
    assert "output byte limit" in output


@pytest.mark.parametrize(
    "value",
    [
        "export BUILDBUDDY_API_KEY=abcdefghijk",
        "curl --token abcdefghijk",
        "https://user:abcdefghijk@host.invalid",
        "https://host.invalid?X-Amz-Signature=abcdefghijk",
    ],
)
def test_common_credential_shapes_are_redacted(value):
    assert "abcdefghijk" not in ci_log.redact(value)


def test_rpc_oversize_and_http_failure_do_not_echo_remote_body(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, bound):
            return b"x" * bound

    monkeypatch.setattr(
        ci_log.urllib.request,
        "build_opener",
        lambda *_: SimpleNamespace(open=lambda *_a, **_kw: Response()),
    )
    with pytest.raises(ci_log.EvidenceError, match="byte bound"):
        ci_log.fetch_chunk(INVOCATION, "")

    def refused(*_args, **_kwargs):
        raise OSError("remote-body-with-private-output")

    monkeypatch.setattr(
        ci_log.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=refused)
    )
    with pytest.raises(
        ci_log.EvidenceError, match="^Log read failed; no CI outcome inferred$"
    ):
        ci_log.fetch_chunk(INVOCATION, "")


def test_test_counts_survive_later_publication_output():
    text = "Executed 0 out of 50 tests: 50 tests pass.\n" + "publishing image\n" * 500
    output = ci_log.excerpts(text)
    assert "Executed 0 out of 50 tests: 50 tests pass." in output
    assert output.endswith("publishing image\n")


def test_test_counts_are_protected_after_error_budget_is_exhausted():
    text = "".join(f"ERROR {i}: " + "x" * 100 + "\n" for i in range(1500))
    text += "Executed 0 out of 50 tests: 50 tests pass.\n"
    text += "publishing image\n" * 500
    output = ci_log.excerpts(text)
    assert "Executed 0 out of 50 tests: 50 tests pass." in output
    assert "ERROR 0:" in output and "publishing image" in output
    assert len(output.encode()) < ci_log.MAX_OUTPUT


def test_protected_test_count_budget_stays_bounded_and_labels_omissions():
    text = "".join(
        f"Executed {i} out of 2000 tests: 2000 tests pass.\n" for i in range(2000)
    )
    text += "publishing image\n" * 500
    output = ci_log.excerpts(text)
    assert "Executed 1999 out of 2000 tests: 2000 tests pass." in output
    assert "some test-count summaries omitted: summary byte limit" in output
    assert len(output.encode()) < ci_log.MAX_OUTPUT


def test_absent_log_text_is_explicit_not_a_complete_log():
    output, _state = ci_log.inspect(
        "owner/repo", SHA, status_reader=lambda *_: STATUS, fetch=lambda *_: {}
    )
    assert "no stored log text available" in output
    assert "available stored log scanned" not in output


@pytest.mark.parametrize(
    "state,expected", [("success", 0), ("failure", 1), ("error", 1), ("pending", 2)]
)
def test_cli_exit_is_status_not_test_count(monkeypatch, capsys, state, expected):
    monkeypatch.setattr(
        ci_log,
        "inspect",
        lambda *_args, **_kwargs: ("Executed 0 out of 50 tests\n", state),
    )
    monkeypatch.setattr(ci_log, "cache_directory", lambda: None)
    assert ci_log.main([SHA]) == expected
    assert "Executed 0" in capsys.readouterr().out


def test_guest_image_ships_the_executable_and_registered_test():
    build = Path(__file__).with_name("BUILD").read_text()
    assert (
        "./usr/local/bin/ci-log type=file content=$(execpath ci_log.py) mode=0755"
        in build
    )
    assert '":ci_log_tar",' in build
    assert 'name = "ci_log_test"' in build
