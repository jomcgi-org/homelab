"""Unit tests for the goosecracker delivery/publish path (ADR 024) and WS2/WS3.

Covers the artifact publish + Discord message shaping, the default mirror wiring
(WS2: GOOSECRACKER_GIT_MIRROR defaults gitMirror when caller omits it), and the
recorded-ref line in delivery messages (WS3). Also covers _drain_agent_queue,
the conversational-queue drain added for /agent thread continuations.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from goosecracker import runner

_GOOSE_RESULT = (
    "  ▸ write path /tmp/artifact.html\n"
    "```goose-result\n"
    "type: note\n"
    "summary: A bouncing ball demo with gravity.\n"
    "```\n"
)


def test_extract_summary_pulls_summary_line():
    assert (
        runner._extract_summary(_GOOSE_RESULT) == "A bouncing ball demo with gravity."
    )


def test_extract_summary_empty_when_absent():
    assert runner._extract_summary("no result block here") == ""


_AGENT_RESULT = (
    "Loading recipe: Agent\n"
    "  ▸ shell git status\n"
    "```goose-result\n"
    "type: note\n"
    "url: <artifact URL, if any>\n"
    "summary: Added a null check in parse().\n"
    "```\n"
)

_AGENT_RESULT_WITH_PR = (
    "```goose-result\n"
    "type: pr\n"
    "url: https://github.com/jomcgi/homelab/pull/42\n"
    "summary: Opened a PR fixing the parser.\n"
    "```\n"
)


async def test_delivery_message_agent_posts_summary_not_transcript():
    data = {"result": _AGENT_RESULT, "recordedRef": "refs/agents/s-1"}
    msg = await runner._delivery_message("s-1", "agent", data)
    assert "Added a null check in parse()." in msg
    # the raw transcript (recipe banner + tool lines) must NOT be dumped
    assert "▸ shell" not in msg
    assert "Loading recipe" not in msg
    assert "recorded: refs/agents/s-1" in msg
    # the recipe's placeholder url is not a real link, so it is not appended
    assert "<artifact URL" not in msg


async def test_delivery_message_agent_appends_real_pr_url():
    msg = await runner._delivery_message(
        "s-2", "agent", {"result": _AGENT_RESULT_WITH_PR}
    )
    assert "Opened a PR fixing the parser." in msg
    assert "https://github.com/jomcgi/homelab/pull/42" in msg


async def test_delivery_message_agent_falls_back_to_transcript_without_block():
    msg = await runner._delivery_message(
        "s-3", "agent", {"result": "just some raw output, no result block"}
    )
    assert "just some raw output" in msg


async def test_delivery_message_agent_posts_trailing_narrative():
    # A question answered with a clean narrative (no goose-result block, no tool
    # chrome): the answer is goose's trailing narrative, so post that (banner
    # stripped), not the head of the transcript.
    result = (
        "Loading recipe: Agent\n"
        "  __( O)>  goose is ready\n"
        "Joe has been working on the git mirror and the /agent command this week."
    )
    msg = await runner._delivery_message("s-9", "agent", {"result": result})
    assert "Joe has been working on the git mirror" in msg
    assert "Loading recipe" not in msg  # recipe banner stripped
    assert "goose is ready" not in msg  # no terminal chrome leaks


async def test_delivery_message_agent_suppresses_raw_transcript():
    # The incident: a resume turn ran without the response schema, so no structured
    # JSON and no goose-result block. goose spent the run cat-ing files (including
    # runner.py, whose source literally contains the "goose is ready" marker) and
    # never wrote a clean answer. Delivery must NOT ship the raw transcript, and the
    # old rfind bug must not slice into the cat-ed source (it anchored on the marker
    # inside runner.py's own source).
    result = (
        "  __( O)>  goose is ready\n"
        "  ────────────────────────────────\n"
        "  ▸ shell\n"
        "    command: cat runner.py\n"
        '    marker = "goose is ready"\n'
        "    idx = result.rfind(marker)\n"
        "    body = result[idx + len(marker) :]\n"
        "  ────────────────────────────────\n"
        "Now I have a thorough picture. Let me compile the answer."
    )
    msg = await runner._delivery_message("s-12", "agent", {"result": result})
    assert "rfind(marker)" not in msg  # did not ship (or slice into) the cat-ed source
    assert "▸" not in msg
    assert "goose is ready" not in msg
    assert "couldn't produce a clean answer" in msg


async def test_delivery_message_agent_prefers_typed_response():
    # The agent recipe's response.json_schema makes goose emit a JSON object as
    # its last line; delivery posts its summary + details + real url, not the
    # transcript.
    result = (
        "Loading recipe: Agent\n"
        "  ▸ shell git status\n"
        '{"type": "pr", "summary": "Fixed the parser null check.", '
        '"details": "Added a guard in parse() and a regression test.", '
        '"url": "https://github.com/jomcgi/homelab/pull/99"}'
    )
    msg = await runner._delivery_message("s-10", "agent", {"result": result})
    assert "Fixed the parser null check." in msg
    assert "Added a guard in parse()" in msg
    assert "https://github.com/jomcgi/homelab/pull/99" in msg
    assert "▸ shell" not in msg  # transcript not dumped


async def test_delivery_message_agent_typed_response_summary_only():
    result = '{"type": "answer", "summary": "There were 3 commits today."}'
    msg = await runner._delivery_message("s-11", "agent", {"result": result})
    assert "There were 3 commits today." in msg


def test_parse_structured_result_none_when_no_trailing_json():
    assert runner._parse_structured_result("just prose, no json") is None
    assert runner._parse_structured_result("{ not valid json }") is None
    assert runner._parse_structured_result('{"summary": "ok"}')["summary"] == "ok"


async def test_delivery_message_publishes_and_links(monkeypatch):
    monkeypatch.setattr(
        runner, "_publish_artifact", lambda s, h: "https://jomcgi.dev/artifact/abc123"
    )
    data = {"artifactHtml": "<html>x</html>", "result": _GOOSE_RESULT}

    msg = await runner._delivery_message("sess", "artifact", data)

    assert "https://jomcgi.dev/artifact/abc123" in msg
    assert "A bouncing ball demo with gravity." in msg
    # the raw transcript (the write tool line) must NOT be dumped
    assert "▸ write path" not in msg


async def test_delivery_message_artifact_run_with_no_artifact():
    msg = await runner._delivery_message(
        "sess", "artifact", {"result": "chatted instead"}
    )
    assert "no artifact" in msg.lower()


async def test_delivery_message_publish_failure_is_reported(monkeypatch):
    def boom(_s, _h):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(runner, "_publish_artifact", boom)
    msg = await runner._delivery_message(
        "sess", "artifact", {"artifactHtml": "<html>x</html>"}
    )
    assert "failed" in msg.lower()


async def test_delivery_message_non_artifact_posts_result():
    msg = await runner._delivery_message("sess", "agent", {"result": "hello from qwen"})
    assert msg == "hello from qwen"


# ---------------------------------------------------------------------------
# WS2: default mirror wiring via _effective_mirror_ref
# ---------------------------------------------------------------------------


def test_effective_mirror_empty_when_no_repo(monkeypatch):
    """No repo and no explicit mirror -> empty mirror (repo-less run, no clone).

    There is no implicit default repo (ADR 029): an omitted repo is the
    artifact/no-checkout path, so the handler skips the clone.
    """
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("", "")
    assert m == ""
    assert r == "main"


def test_effective_mirror_caller_override_wins(monkeypatch):
    """Explicit git_mirror from the caller takes precedence over the env default."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("git://other:9418/loom", "feat/x")
    assert m == "git://other:9418/loom"
    assert r == "feat/x"


def test_effective_mirror_empty_when_env_unset(monkeypatch):
    """When GOOSECRACKER_GIT_MIRROR is unset, effective mirror is empty (no clone)."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "")
    m, r = runner._effective_mirror_ref("", "")
    assert m == ""
    assert r == "main"


def test_effective_mirror_ref_defaults_to_main_when_only_mirror_set(monkeypatch):
    """git_ref defaults to 'main' when caller omits it but mirror is specified."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("", "")
    assert r == "main"


# ---------------------------------------------------------------------------
# repo param: _effective_mirror_ref selects the right repo under the base
# ---------------------------------------------------------------------------


def test_effective_mirror_custom_repo_appended(monkeypatch):
    """An owner/repo name is appended under the base; the slash just nests the
    git:// path (ADR 029 naming convention)."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("", "", "colincee/homelab")
    assert m == "git://mirror:9418/colincee/homelab"
    assert r == "main"


def test_effective_mirror_empty_repo_skips_clone(monkeypatch):
    """repo='' -> empty mirror: no implicit default, the handler skips the clone."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("", "", "")
    assert m == ""
    assert r == "main"


def test_effective_mirror_explicit_git_mirror_overrides_repo(monkeypatch):
    """Explicit git_mirror always wins; repo is ignored when mirror is set."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("git://other:9418/explicit", "", "loom")
    assert m == "git://other:9418/explicit"


# ---------------------------------------------------------------------------
# WS3: _delivery_message appends recorded ref
# ---------------------------------------------------------------------------


async def test_delivery_message_appends_recorded_ref_for_agent():
    """A recorded scratch ref is appended to the agent result message."""
    data = {"result": "did the work", "recordedRef": "refs/agents/sess-abc"}
    msg = await runner._delivery_message("sess-abc", "agent", data)
    assert "did the work" in msg
    assert "recorded: refs/agents/sess-abc" in msg


async def test_delivery_message_appends_recorded_ref_for_artifact(monkeypatch):
    """A recorded ref is also appended when the run produced an artifact."""
    monkeypatch.setattr(
        runner, "_publish_artifact", lambda s, h: "https://jomcgi.dev/artifact/x"
    )
    data = {
        "artifactHtml": "<html/>",
        "result": "",
        "recordedRef": "refs/agents/sess-art",
    }
    msg = await runner._delivery_message("sess-art", "artifact", data)
    assert "https://jomcgi.dev/artifact/x" in msg
    assert "recorded: refs/agents/sess-art" in msg


async def test_delivery_message_no_recorded_ref_unchanged():
    """When no recordedRef is present, the message is unchanged."""
    msg = await runner._delivery_message("sess", "agent", {"result": "result text"})
    assert msg == "result text"
    assert "recorded:" not in msg


def test_split_message_short_content_single_page():
    assert runner._split_message("short answer") == ["short answer"]


def test_split_message_pages_long_content_on_lines():
    line = "x" * 400
    content = "\n".join([line] * 12)  # ~4900 chars over several lines
    pages = runner._split_message(content)
    assert len(pages) >= 2
    assert all(len(p) <= runner._MAX_DISCORD for p in pages)
    # every original line survives across the pages (nothing dropped)
    assert "\n".join(pages).count("x" * 400) == 12


def test_split_message_hard_splits_an_overlong_line():
    pages = runner._split_message("y" * 4000)
    assert len(pages) >= 2
    assert all(len(p) <= runner._MAX_DISCORD for p in pages)


# ---------------------------------------------------------------------------
# _post_agent_run: transient-failure retry (fc-invoke single-replica rollout gap)
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for httpx.AsyncClient that replays a scripted POST sequence.

    Each POST pops the next scripted item: an httpx.Response is returned (the
    caller runs raise_for_status/json on it), an Exception is raised.
    """

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(status, *, json_body=None, text=""):
    req = httpx.Request("POST", "http://fc-invoke/invoke/agent/s")
    if json_body is not None:
        return httpx.Response(status, request=req, json=json_body)
    return httpx.Response(status, request=req, text=text)


def _patch(monkeypatch, script):
    """Point runner.httpx.AsyncClient at one shared fake and no-op the backoff.

    Returns (fake_client, sleeps) so a test can assert call and backoff counts.
    """
    fake = _FakeClient(script)
    monkeypatch.setattr(runner.httpx, "AsyncClient", lambda **kw: fake)
    sleeps: list[float] = []

    async def _fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(runner.asyncio, "sleep", _fake_sleep)
    return fake, sleeps


async def test_post_agent_run_retries_504_then_succeeds(monkeypatch):
    fake, sleeps = _patch(
        monkeypatch,
        [
            _resp(504, text="gateway timeout"),
            _resp(200, json_body={"status": "ok", "result": "done"}),
        ],
    )
    retries = []
    data = await runner._post_agent_run(
        "http://fc-invoke/invoke/agent/s",
        {"task": "x"},
        lambda attempt, wait, reason: retries.append((attempt, wait, reason)),
    )
    assert data == {"status": "ok", "result": "done"}
    assert fake.calls == 2
    assert retries == [(1, runner._RETRY_BASE, "HTTP 504")]
    assert sleeps == [runner._RETRY_BASE]  # one backoff of the base delay


async def test_post_agent_run_retries_connect_error(monkeypatch):
    fake, _ = _patch(
        monkeypatch,
        [
            httpx.ConnectError("connection refused"),
            _resp(200, json_body={"status": "ok"}),
        ],
    )
    reasons = []
    data = await runner._post_agent_run(
        "http://fc/s", {}, lambda attempt, wait, reason: reasons.append(reason)
    )
    assert data == {"status": "ok"}
    assert fake.calls == 2
    assert reasons == ["connection failed"]


async def test_post_agent_run_no_retry_on_read_timeout(monkeypatch):
    # A read timeout means goose was accepted and ran long; a retry would spawn
    # a duplicate, so it must surface immediately.
    fake, _ = _patch(monkeypatch, [httpx.ReadTimeout("read timed out")])
    retries = []
    with pytest.raises(RuntimeError, match="could not reach fc-invoke"):
        await runner._post_agent_run("http://fc/s", {}, lambda *a: retries.append(a))
    assert fake.calls == 1
    assert retries == []


async def test_post_agent_run_no_retry_on_non_transient_status(monkeypatch):
    fake, _ = _patch(monkeypatch, [_resp(400, text="bad request")])
    retries = []
    with pytest.raises(RuntimeError, match="fc-invoke returned HTTP 400"):
        await runner._post_agent_run("http://fc/s", {}, lambda *a: retries.append(a))
    assert fake.calls == 1
    assert retries == []


async def test_post_agent_run_gives_up_after_deadline(monkeypatch):
    # With a zero deadline the first transient failure exhausts the budget, so
    # the loop raises instead of sleeping or calling back.
    monkeypatch.setattr(runner, "_RETRY_DEADLINE", 0.0)
    fake, sleeps = _patch(monkeypatch, [_resp(504, text="gateway timeout")])
    retries = []
    with pytest.raises(RuntimeError, match="fc-invoke returned HTTP 504"):
        await runner._post_agent_run("http://fc/s", {}, lambda *a: retries.append(a))
    assert fake.calls == 1
    assert sleeps == []
    assert retries == []


# ---------------------------------------------------------------------------
# run_and_deliver conversational loop (the orphan-free next-turn dispatch)
# ---------------------------------------------------------------------------


async def test_run_and_deliver_runs_drained_turn_in_loop(monkeypatch):
    """The drained next turn runs inside the same run_and_deliver call (awaited),
    not via a create_task that asyncio.run would cancel on teardown. Also drives
    the reaction lifecycle: mark_inflight_running each turn, ack_inflight at each
    turn's end."""
    turns = []

    async def fake_turn(session, **kwargs):
        turns.append(kwargs["task"])
        return True

    monkeypatch.setattr(runner, "_run_one_turn", fake_turn)
    monkeypatch.setattr(runner.threads, "upsert_run", MagicMock())

    fake_api = MagicMock()
    # One queued batch, then the queue is empty.
    fake_api.drain_agent_queue = MagicMock(side_effect=[("second turn", ["m1"]), None])

    with patch.dict(sys.modules, {"chat.api": fake_api}):
        await runner.run_and_deliver(
            "sess",
            task="first turn",
            recipe="agent",
            tier="",
            git_mirror="",
            git_ref="",
            discord_thread="T",
        )

    assert turns == ["first turn", "second turn"]  # both ran in one call
    assert fake_api.mark_inflight_running.call_count == 2
    fake_api.ack_inflight.assert_any_call("T", True)
    assert fake_api.ack_inflight.call_count == 2


async def test_run_and_deliver_non_agent_runs_once(monkeypatch):
    """A non-agent (artifact) run does one turn and never touches the queue."""
    turns = []

    async def fake_turn(session, **kwargs):
        turns.append(kwargs["task"])
        return True

    monkeypatch.setattr(runner, "_run_one_turn", fake_turn)
    fake_api = MagicMock()

    with patch.dict(sys.modules, {"chat.api": fake_api}):
        await runner.run_and_deliver(
            "sess",
            task="build it",
            recipe="artifact",
            tier="artifact",
            git_mirror="",
            git_ref="",
            discord_thread="T",
        )

    assert turns == ["build it"]
    fake_api.drain_agent_queue.assert_not_called()


async def test_run_and_deliver_idles_thread_on_bookkeeping_error(monkeypatch):
    """If post-turn bookkeeping raises (a DB blip), the loop force-idles the
    thread and stops instead of dying with running=True (which would wedge the
    thread for the full stale timeout)."""
    turns = []

    async def fake_turn(session, **kwargs):
        turns.append(kwargs["task"])
        return True

    monkeypatch.setattr(runner, "_run_one_turn", fake_turn)

    fake_api = MagicMock()
    fake_api.ack_inflight = MagicMock(side_effect=RuntimeError("db blip"))

    with patch.dict(sys.modules, {"chat.api": fake_api}):
        await runner.run_and_deliver(
            "sess",
            task="first turn",
            recipe="agent",
            tier="",
            git_mirror="",
            git_ref="",
            discord_thread="T",
        )

    assert turns == ["first turn"]  # ran the turn, then stopped cleanly
    fake_api.force_idle_thread.assert_called_once_with("T")
    fake_api.drain_agent_queue.assert_not_called()
