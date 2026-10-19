"""Only bounded, complete primary sources enter a review's evidence bundle."""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from knowledge import review_coverage as coverage
from knowledge.review_sources import (
    MAX_BYTES,
    EvidenceUnavailable,
    EvidenceUnsupported,
    collect_sources,
)

REPO = "jomcgi-org/homelab"
SHA = "a" * 40


def test_pr_evidence_covers_state_head_statuses_and_checks_without_report_body():
    calls = []

    def fetch(path):
        calls.append(path)
        if "/issues/" in path:
            return {
                "number": 1,
                "state": "open",
                "pull_request": {},
                "body": "untrusted claim",
                "labels": [],
            }
        if "/pulls/" in path:
            return {
                "number": 1,
                "state": "open",
                "head": {"sha": SHA},
                "draft": True,
                "merged": False,
            }
        if "/status?" in path:
            return {
                "sha": SHA,
                "total_count": 1,
                "state": "success",
                "statuses": [{"context": "pr-checks", "state": "success"}],
            }
        return {"total_count": 0, "check_runs": []}

    sources, observed = collect_sources(
        {"title": "PR #1 is reported open", "content": None}, fetch=fetch
    )
    assert len(calls) == len(sources) == 4 and observed is not None
    assert "untrusted claim" not in str(sources)
    assert "pr-checks" in sources[2]["text"]
    assert {s["subject"] for s in sources} == {"github:1"}


def test_files_are_pinned_to_one_main_commit():
    calls = []

    def fetch(path):
        calls.append(path)
        if "/git/ref/" in path:
            return {"object": {"sha": SHA}}
        assert path.endswith("?ref=" + SHA)
        return {
            "type": "file",
            "path": path.split("/contents/", 1)[1].split("?", 1)[0],
            "encoding": "base64",
            "size": 7,
            "content": base64.b64encode(b"content").decode(),
        }

    sources, _ = collect_sources(
        {
            "title": "The contract lives in projects/app/main.py:4 and docs/guide.md",
            "content": None,
        },
        fetch=fetch,
    )
    assert len(calls) == 3 and len(sources) == 2
    assert all("/blob/" + SHA + "/" in item["url"] for item in sources)


@pytest.mark.parametrize(
    "claim",
    [
        "https://github.com/other/repo/pull/1 is open",
        "PR #1 is open and other/repo#2 is closed",
        "PR #1 is open and https://github.com/other/repo/blob/main/projects/app.py is current",
        "PR #1 and PR #2 and PR #3 and PR #4",
        "projects/../private/key.py",
    ],
)
def test_foreign_refs_excess_refs_and_traversal_never_fetch(claim):
    with pytest.raises(EvidenceUnavailable):
        collect_sources(
            {"title": claim, "content": None}, fetch=lambda _: pytest.fail("fetched")
        )


def test_missing_sources_are_explicit_and_do_not_invent_evidence():
    assert collect_sources(
        {"title": "The system is wonderful", "content": None},
        fetch=lambda _: pytest.fail("fetched"),
    ) == ([], None)


@pytest.mark.parametrize(
    "broken", ["identity", "head", "state", "pagination", "statuses", "check_sha"]
)
def test_incomplete_or_malformed_source_never_reaches_reviewer(broken):
    def fetch(path):
        if "/issues/" in path:
            return {
                "number": 2 if broken == "identity" else 1,
                "state": "open",
                "pull_request": {},
            }
        if "/pulls/" in path:
            return {
                "number": 1,
                "state": "unknown" if broken == "state" else "open",
                "draft": False,
                "merged": False,
                "head": {"sha": "bad" if broken == "head" else SHA},
            }
        if "/status?" in path:
            return {
                "sha": SHA,
                "total_count": 2 if broken == "statuses" else 0,
                "statuses": [],
            }
        if broken == "check_sha":
            return {"total_count": 1, "check_runs": [{"head_sha": "b" * 40}]}
        return {"total_count": 101 if broken == "pagination" else 0, "check_runs": []}

    with pytest.raises(EvidenceUnavailable):
        collect_sources({"title": "PR #1 is open", "content": None}, fetch=fetch)


def test_oversized_evidence_fails_instead_of_silently_truncating():
    def fetch(path):
        return {"number": 1, "state": "open", "title": "x" * 50_000}

    with pytest.raises(EvidenceUnavailable, match="byte bound"):
        collect_sources({"title": "Issue #1 is open", "content": None}, fetch=fetch)


REAL_PATH = "projects/monolith/knowledge/extraction.py"


def blob_fetch(raw, *, path=REAL_PATH, revision=SHA, calls=None, mutate=None):
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    def fetch(url):
        if calls is not None:
            calls.append(url)
        if "/git/ref/" in url:
            return {"object": {"sha": revision}}
        if "/git/trees/" in url:
            assert f"/git/trees/{revision}:" in url
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": Path(path).name,
                        "sha": sha,
                        "size": len(raw),
                        "type": "blob",
                        "mode": "100644",
                    }
                ],
            }
        assert url.endswith("/git/blobs/" + sha)
        result = {
            "sha": sha,
            "size": len(raw),
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }
        return mutate(result) if mutate else result

    return fetch


def literal_claim(text="RAW_BODY_CAP = 60_000", path=REAL_PATH):
    return {
        "title": f"`{path}` contains the exact text `{text}`.",
        "content": None,
        "scope": "repo:" + REPO,
    }


def test_real_oversized_file_has_exact_pinned_witness_and_explicit_omissions(
    monkeypatch,
):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    raw = Path(__file__).with_name("extraction.py").read_bytes()
    assert len(raw) > MAX_BYTES
    calls = []
    claim = literal_claim()
    sources, observed = collect_sources(claim, fetch=blob_fetch(raw, calls=calls))
    source = sources[0]
    span = source["coverage"]
    assert source["text"] == "RAW_BODY_CAP = 60_000"
    assert raw[span["start_byte"] : span["end_byte"]].decode() == source["text"]
    assert span["omitted_bytes"] == [
        [0, span["start_byte"]],
        [span["end_byte"], len(raw)],
    ]
    assert f"/blob/{SHA}/" in source["url"] and "#L" in source["url"]
    assert len(calls) == 3 and observed
    assert coverage.serialized_size(sources) <= MAX_BYTES


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Extraction limits inputs", "content": REAL_PATH},
        {"content": "This also proves that no other paths bypass the cap."},
        {"content": "There is no larger limit in this file."},
        {"title": f"`{REAL_PATH}` contains the exact text `invented_missing_literal`."},
        {
            "content": f"`{REAL_PATH}` contains the exact text `invented_missing_literal`."
        },
    ],
)
def test_partial_negative_and_incomplete_claims_fail_closed(monkeypatch, change):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    raw = Path(__file__).with_name("extraction.py").read_bytes()
    with pytest.raises(EvidenceUnsupported, match="incomplete coverage") as error:
        collect_sources(literal_claim() | change, fetch=blob_fetch(raw))
    assert error.value.identity["files"][REAL_PATH]["size"] == len(raw)


def test_off_path_keeps_legacy_size_gate(monkeypatch):
    monkeypatch.delenv("KG_FACT_REVIEW_COVERAGE_ENABLED", raising=False)
    raw = Path(__file__).with_name("extraction.py").read_bytes()

    def fetch(url):
        if "/git/ref/" in url:
            return {"object": {"sha": SHA}}
        return {
            "type": "file",
            "path": REAL_PATH,
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode(),
        }

    with pytest.raises(EvidenceUnavailable) as error:
        collect_sources(literal_claim(), fetch=fetch)
    assert type(error.value) is EvidenceUnavailable


def test_exact_serialized_boundary_and_utf8_witness_offsets(monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    path = "projects/a.py"
    overhead = coverage.serialized_size(
        [
            {
                "id": "s1",
                "subject": "file:" + path,
                "url": f"https://github.com/{REPO}/blob/{SHA}/{path}",
                "text": "",
            }
        ]
    )
    for extra in (0, 1):
        raw = b"x" * (MAX_BYTES - overhead + extra)
        claim = {"title": "File " + path}
        if extra:
            with pytest.raises(EvidenceUnsupported):
                collect_sources(claim, fetch=blob_fetch(raw, path=path))
        else:
            sources, _ = collect_sources(claim, fetch=blob_fetch(raw, path=path))
            assert coverage.serialized_size(sources) == MAX_BYTES
    # A below-48KB file can exceed the serialized bound through JSON escaping.
    literal = 'A literal with "quotes" and café'
    raw = ("é" * 9000 + literal + "é" * 9000).encode()
    assert len(raw) < MAX_BYTES
    sources, _ = collect_sources(literal_claim(literal), fetch=blob_fetch(raw))
    span = sources[0]["coverage"]
    assert span["start_byte"] == 18000
    assert raw[span["start_byte"] : span["end_byte"]].decode() == literal


@pytest.mark.parametrize(
    "mutation,deterministic",
    [
        (lambda body: body | {"encoding": "none"}, True),
        (lambda body: body | {"content": "!!!!"}, True),
        (lambda body: body | {"sha": "b" * 40}, False),
        (lambda body: body | {"size": body["size"] + 1}, False),
        (lambda body: body | {"content": "YQ=="}, False),
        (lambda body: body | {"content": "a" * 200_000}, True),
    ],
)
def test_malformed_blob_cannot_become_evidence(monkeypatch, mutation, deterministic):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    with pytest.raises(EvidenceUnavailable) as error:
        collect_sources(
            literal_claim(), fetch=blob_fetch(b"source data", mutate=mutation)
        )
    assert isinstance(error.value, EvidenceUnsupported) is deterministic


@pytest.mark.parametrize(
    "raw", [b"\xff", b"binary\x00data", b"a" * (coverage.MAX_FILE_BYTES + 1)]
)
def test_binary_and_decode_budget_fail_deterministically(monkeypatch, raw):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    with pytest.raises(EvidenceUnsupported) as error:
        collect_sources(literal_claim(), fetch=blob_fetch(raw))
    assert error.value.identity


def test_streaming_bounds_wire_decoding_and_json_expansion():
    import httpx

    class Stream(httpx.SyncByteStream):
        def __init__(self, raw):
            self.raw = raw

        def __iter__(self):
            for start in range(0, len(self.raw), 8192):
                yield self.raw[start : start + 8192]

    for raw in (
        b" " * (coverage.MAX_RESPONSE_BYTES + 1),
        b"\xff",
        json.dumps({"x": "é" * 40_000}, ensure_ascii=False).encode(),
    ):
        with (
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request, body=raw: httpx.Response(200, stream=Stream(body))
                )
            ) as client,
            pytest.raises(EvidenceUnsupported),
        ):
            coverage.read_response(client, "/repos/test/repo")


def test_duplicate_references_and_all_positive_clauses_are_covered(monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    raw = Path(__file__).with_name("extraction.py").read_bytes()
    claim = literal_claim() | {
        "content": literal_claim()["title"]
        + "\n"
        + literal_claim("DOC_DRIFT_CAP = 10")["title"]
    }
    calls = []
    sources, _ = collect_sources(claim, fetch=blob_fetch(raw, calls=calls))
    assert len(calls) == 3
    assert [s["text"] for s in sources] == [
        "RAW_BODY_CAP = 60_000",
        "DOC_DRIFT_CAP = 10",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"truncated": True},
        {
            "tree": [
                {
                    "path": "extraction.py",
                    "sha": "a" * 40,
                    "size": 1,
                    "type": "blob",
                    "mode": "120000",
                }
            ]
        },
        {
            "tree": [
                {
                    "path": "extraction.py",
                    "sha": "a" * 40,
                    "type": "tree",
                    "mode": "040000",
                }
            ]
        },
    ],
)
def test_unsupported_tree_or_file_type_has_a_durable_identity(monkeypatch, change):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    fetch = blob_fetch(b"source")

    def read(path):
        result = fetch(path)
        return result | change if "/git/trees/" in path else result

    with pytest.raises(EvidenceUnsupported) as error:
        collect_sources(literal_claim(), fetch=read)
    assert REAL_PATH in error.value.identity["files"]


def test_enabled_request_bound_is_deterministic_but_deadline_is_transient(monkeypatch):
    from knowledge import review_sources

    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    monkeypatch.setattr(review_sources, "MAX_REQUESTS", 2)
    with pytest.raises(EvidenceUnsupported, match="request bound") as error:
        collect_sources(literal_claim(), fetch=blob_fetch(b"source"))
    assert error.value.identity
    monkeypatch.setattr(review_sources, "DEADLINE_SECONDS", 0)
    with pytest.raises(EvidenceUnavailable) as error:
        collect_sources(
            literal_claim(), fetch=lambda _: pytest.fail("deadline fetched")
        )
    assert type(error.value) is EvidenceUnavailable


def test_enabled_foreign_claim_does_not_reach_metadata_probe(monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    with pytest.raises(EvidenceUnsupported, match="another repository"):
        collect_sources(
            literal_claim()
            | {"content": "https://github.com/foreign/repo/blob/main/projects/a.py"},
            fetch=lambda _: pytest.fail("foreign fetch"),
        )


def test_pointers_inside_a_literal_are_not_extra_source_subjects(monkeypatch):
    monkeypatch.setenv("KG_FACT_REVIEW_COVERAGE_ENABLED", "true")
    literal = "PR #1 names projects/other/source.py"
    raw = ("# padding\n" * 8000 + literal).encode()
    calls = []
    sources, _ = collect_sources(
        literal_claim(literal), fetch=blob_fetch(raw, calls=calls)
    )
    assert len(calls) == 3
    assert len(sources) == 1 and sources[0]["text"] == literal
