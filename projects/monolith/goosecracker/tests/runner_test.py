"""Unit tests for the goosecracker delivery/publish path (ADR 024) and WS2/WS3.

Covers the artifact publish + Discord message shaping, the default mirror wiring
(WS2: GOOSECRACKER_GIT_MIRROR defaults gitMirror when caller omits it), and the
recorded-ref line in delivery messages (WS3). Also covers _drain_agent_queue,
the conversational-queue drain added for /agent thread continuations.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

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


def test_subrecipe_bodies_loads_every_catalog_id_from_runfiles():
    """The runner reads every catalog sub-recipe body from runfiles for injection.

    Guards the cross-package data dep (goosecracker_runner_test data ->
    :recipe_yamls) and the parents[] runfiles hop in ``_GUEST_RECIPES_DIR``: a
    missing data dep or a wrong path fails here in CI, not at runtime with a
    FileNotFoundError that would kill every agent run.
    """
    import yaml

    from goosecracker.recipe_catalog import CATALOG

    runner._subrecipe_bodies.cache_clear()
    bodies = runner._subrecipe_bodies()
    assert set(bodies) == {f"{rid}.yaml" for rid in CATALOG}
    for name, body in bodies.items():
        parsed = yaml.safe_load(body)
        assert isinstance(parsed, dict) and parsed.get("instructions"), name


def test_router_delegate_paths_match_injected_bodies():
    """Every path the fallback router delegates to is a body the runner injects.

    The naming contract binds render_fallback_router() to _subrecipe_bodies():
    the router points ``delegate`` at ``/injected-context/<id>.yaml`` and the
    runner must inject a body under that exact basename, or the guest delegates
    to a file that was never staged.
    """
    import yaml

    from goosecracker.router_render import render_fallback_router

    runner._subrecipe_bodies.cache_clear()
    keys = set(runner._subrecipe_bodies())
    doc = yaml.safe_load(render_fallback_router())
    for sub in doc["sub_recipes"]:
        assert sub["path"] == f"/injected-context/{sub['name']}.yaml"
        assert f"{sub['name']}.yaml" in keys


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
    msg = await runner._delivery_message("s-1", data)
    assert "Added a null check in parse()." in msg
    # the raw transcript (recipe banner + tool lines) must NOT be dumped
    assert "▸ shell" not in msg
    assert "Loading recipe" not in msg
    assert "recorded: refs/agents/s-1" in msg
    # the recipe's placeholder url is not a real link, so it is not appended
    assert "<artifact URL" not in msg


async def test_delivery_message_agent_appends_real_pr_url():
    msg = await runner._delivery_message("s-2", {"result": _AGENT_RESULT_WITH_PR})
    assert "Opened a PR fixing the parser." in msg
    assert "https://github.com/jomcgi/homelab/pull/42" in msg


async def test_delivery_message_agent_falls_back_to_transcript_without_block():
    msg = await runner._delivery_message(
        "s-3", {"result": "just some raw output, no result block"}
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
    msg = await runner._delivery_message("s-9", {"result": result})
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
    msg = await runner._delivery_message("s-12", {"result": result})
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
    msg = await runner._delivery_message("s-10", {"result": result})
    assert "Fixed the parser null check." in msg
    assert "Added a guard in parse()" in msg
    assert "https://github.com/jomcgi/homelab/pull/99" in msg
    assert "▸ shell" not in msg  # transcript not dumped


async def test_delivery_message_agent_typed_response_summary_only():
    result = '{"type": "answer", "summary": "There were 3 commits today."}'
    msg = await runner._delivery_message("s-11", {"result": result})
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

    msg = await runner._delivery_message("sess", data)

    assert "https://jomcgi.dev/artifact/abc123" in msg
    assert "A bouncing ball demo with gravity." in msg
    # the raw transcript (the write tool line) must NOT be dumped
    assert "▸ write path" not in msg


async def test_delivery_message_publish_failure_is_reported(monkeypatch):
    def boom(_s, _h):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(runner, "_publish_artifact", boom)
    msg = await runner._delivery_message("sess", {"artifactHtml": "<html>x</html>"})
    assert "failed" in msg.lower()


async def test_delivery_message_non_artifact_posts_result():
    msg = await runner._delivery_message("sess", {"result": "hello from qwen"})
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
    msg = await runner._delivery_message("sess-abc", data)
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
    msg = await runner._delivery_message("sess-art", data)
    assert "https://jomcgi.dev/artifact/x" in msg
    assert "recorded: refs/agents/sess-art" in msg


async def test_delivery_message_no_recorded_ref_unchanged():
    """When no recordedRef is present, the message is unchanged."""
    msg = await runner._delivery_message("sess", {"result": "result text"})
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


async def test_run_one_turn_resets_progress_buffer_at_start(monkeypatch):
    """Every turn clears the live-progress buffer before running, so a stale
    done=True left by a prior (streamless) turn cannot render "Done in 0:01" on
    the next turn's message. Verified by spying the reset seam; the turn is forced
    to fail fast (no FC_INVOKE_URL) so no real fc-invoke/DB work runs."""

    reset_spy = MagicMock()
    fake_api = MagicMock()
    fake_api.reset_goosecracker_progress = reset_spy

    monkeypatch.setattr(runner, "FC_INVOKE_URL", "")  # fail fast in the try body
    monkeypatch.setattr(runner.threads, "mark_failed", MagicMock())
    monkeypatch.setattr(runner, "_deliver", AsyncMock())
    monkeypatch.setattr(runner, "_mark_progress_done", MagicMock())

    with patch.dict(sys.modules, {"chat.api": fake_api}):
        ok = await runner._run_one_turn(
            "sess-x",
            task="do it",
            recipe="agent",
            tier="",
            git_mirror="",
            git_ref="",
            discord_thread="T",
        )

    assert ok is False  # fail-fast path (no FC_INVOKE_URL)
    reset_spy.assert_called_once_with("sess-x")  # buffer reset before the turn ran


async def test_run_one_turn_ships_injected_context_in_payload(monkeypatch):
    """ADR 040: the runner rebuilds the injected-context bundle every turn,
    reached through chat.api (not chat internals, per import_boundaries_test),
    and ships it in the fc-invoke payload as "injectedContext" so the guest can
    stage it to /injected-context/ on its ephemeral tmpfs."""
    import chat.api

    captured_payload = {}

    async def fake_post(url, payload, on_retry):
        captured_payload.update(payload)
        return {"status": "ok", "result": "done", "sessionDb": ""}

    monkeypatch.setattr(runner, "_post_agent_run", fake_post)
    monkeypatch.setattr(runner, "FC_INVOKE_URL", "http://fc-invoke")
    monkeypatch.setattr(runner.sessions, "load", MagicMock(return_value=None))
    monkeypatch.setattr(runner.threads, "mark_completed", MagicMock())
    monkeypatch.setattr(runner, "_deliver", AsyncMock())
    monkeypatch.setattr(runner, "_mark_progress_done", MagicMock())
    monkeypatch.setattr(runner, "_persist_session_db", AsyncMock())
    monkeypatch.setattr(chat.api, "reset_goosecracker_progress", MagicMock())
    monkeypatch.setattr(chat.api, "ensure_steering_token", lambda _s: "")
    monkeypatch.setattr(
        chat.api, "build_injected_context", lambda tid, tier="": {"transcript.md": "hi"}
    )

    ok = await runner._run_one_turn(
        "sess-1",
        task="q",
        recipe="agent",
        tier="",
        git_mirror="",
        git_ref="",
        discord_thread="thr-1",
    )

    assert ok is True
    # The ADR 040 per-turn context is shipped. On the agent path the payload also
    # carries the injected fallback router (see
    # test_run_one_turn_without_plan_injects_fallback_router); here we only assert
    # the ADR 040 entry survives alongside it.
    assert captured_payload["injectedContext"]["transcript.md"] == "hi"


async def _run_turn_capturing_payload(monkeypatch, **turn_kwargs):
    """Run a turn with every seam stubbed and return the captured fc-invoke
    payload (mirrors test_run_one_turn_ships_injected_context_in_payload)."""
    import chat.api

    captured_payload = {}

    async def fake_post(url, payload, on_retry):
        captured_payload.update(payload)
        return {"status": "ok", "result": "done", "sessionDb": ""}

    monkeypatch.setattr(runner, "_post_agent_run", fake_post)
    monkeypatch.setattr(runner, "FC_INVOKE_URL", "http://fc-invoke")
    monkeypatch.setattr(runner.sessions, "load", MagicMock(return_value=None))
    monkeypatch.setattr(runner.threads, "mark_completed", MagicMock())
    monkeypatch.setattr(runner, "_deliver", AsyncMock())
    monkeypatch.setattr(runner, "_mark_progress_done", MagicMock())
    monkeypatch.setattr(runner, "_persist_session_db", AsyncMock())
    monkeypatch.setattr(chat.api, "reset_goosecracker_progress", MagicMock())
    monkeypatch.setattr(chat.api, "ensure_steering_token", lambda _s: "")
    monkeypatch.setattr(
        chat.api, "build_injected_context", lambda tid, tier="": {"transcript.md": "hi"}
    )
    ok = await runner._run_one_turn("sess-1", **turn_kwargs)
    assert ok is True
    return captured_payload


async def test_run_one_turn_injects_repo_owner_for_pr_path(monkeypatch):
    """The guest's git origin (the read-only mirror) does not carry its GitHub
    owner/repo, so the runner stages the resolved ADR 029 scope at
    /injected-context/repo for the implement recipe's REST-API PR path."""
    payload = await _run_turn_capturing_payload(
        monkeypatch,
        task="open a PR",
        recipe="agent",
        tier="",
        repo="weave-hand/loom",
        git_mirror="",
        git_ref="",
        discord_thread="thr-1",
    )
    assert payload["injectedContext"]["repo"] == "weave-hand/loom"
    # The ADR 040 per-turn context still rides alongside it.
    assert payload["injectedContext"]["transcript.md"] == "hi"


async def test_run_one_turn_repoless_omits_repo_key(monkeypatch):
    """The repo-less path (e.g. a /agent artifact build with no checkout) injects
    no repo key, so the recipe's presence check is a clean miss and it fails
    loudly rather than opening a PR against the wrong repo."""
    payload = await _run_turn_capturing_payload(
        monkeypatch,
        task="build an artifact",
        recipe="agent",
        tier="",
        repo="",
        git_mirror="",
        git_ref="",
        discord_thread="thr-1",
    )
    assert "repo" not in payload["injectedContext"]


def _one_step_plan():
    """A minimal Plan for the plan-delivery payload tests (Task 6)."""
    from chat.orchestrator_plan import Plan, PlanStep

    return Plan(
        enabled_subrecipes=("query",),
        steps=(PlanStep(sub_recipe="query", context="Answer the question."),),
        done_criteria=("the question is answered",),
    )


async def _run_one_turn_with_captured_payload(monkeypatch, *, plan):
    """Shared setup for the plan-delivery payload tests: stub every seam
    _run_one_turn touches (mirrors test_run_one_turn_ships_injected_context_in_payload)
    and return the captured fc-invoke payload."""
    import chat.api

    captured_payload = {}

    async def fake_post(url, payload, on_retry):
        captured_payload.update(payload)
        return {"status": "ok", "result": "done", "sessionDb": ""}

    monkeypatch.setattr(runner, "_post_agent_run", fake_post)
    monkeypatch.setattr(runner, "FC_INVOKE_URL", "http://fc-invoke")
    monkeypatch.setattr(runner.sessions, "load", MagicMock(return_value=None))
    monkeypatch.setattr(runner.threads, "mark_completed", MagicMock())
    monkeypatch.setattr(runner, "_deliver", AsyncMock())
    monkeypatch.setattr(runner, "_mark_progress_done", MagicMock())
    monkeypatch.setattr(runner, "_persist_session_db", AsyncMock())
    monkeypatch.setattr(chat.api, "reset_goosecracker_progress", MagicMock())
    monkeypatch.setattr(chat.api, "ensure_steering_token", lambda _s: "")
    monkeypatch.setattr(
        chat.api, "build_injected_context", lambda tid, tier="": {"transcript.md": "hi"}
    )

    ok = await runner._run_one_turn(
        "sess-1",
        task="q",
        recipe="agent",
        tier="",
        git_mirror="",
        git_ref="",
        discord_thread="thr-1",
        plan=plan,
    )
    assert ok is True
    return captured_payload


async def test_run_one_turn_with_plan_injects_router_and_plan_file(monkeypatch):
    """Task 6: when a Plan is present, the fc-invoke payload points at the
    injected router recipe and ships both router.yaml and plan.md alongside
    (never replacing) the ADR 040 per-turn injectedContext already built."""
    from goosecracker import router_render

    plan = _one_step_plan()
    payload = await _run_one_turn_with_captured_payload(monkeypatch, plan=plan)

    assert payload["recipe"] == "/injected-context/router.yaml"
    assert payload["injectedContext"]["router.yaml"] == router_render.render_router(
        plan
    )
    assert payload["injectedContext"]["plan.md"] == router_render.render_plan_file(plan)
    # The pre-existing ADR 040 context entry must still be present, not replaced.
    assert payload["injectedContext"]["transcript.md"] == "hi"


async def test_run_one_turn_without_plan_injects_fallback_router(monkeypatch):
    """Plan-less agent path: with no Plan the runner injects the CURRENT
    agent.yaml as the router (render_fallback_router) and points the recipe at
    the injected copy, so a snapshot-resumed thread runs current recipe text
    with the delegate tool instead of its frozen baked agent.yaml. No plan.md is
    injected (there is no ordered plan), and the ADR 040 per-turn context is
    preserved."""
    from goosecracker import router_render

    payload = await _run_one_turn_with_captured_payload(monkeypatch, plan=None)

    assert payload["recipe"] == "/injected-context/router.yaml"
    assert (
        payload["injectedContext"]["router.yaml"]
        == router_render.render_fallback_router()
    )
    assert "plan.md" not in payload["injectedContext"]
    assert payload["injectedContext"]["transcript.md"] == "hi"


# ---------------------------------------------------------------------------
# Conversational reply (_agent_reply_message): rephrase the typed summary in the
# bot's voice using channel context, fail-open to the deterministic summary.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_no_parent_channel(monkeypatch):
    """Keep the coding-agent delivery tests hermetic.

    _agent_reply_message calls chat.api.parent_channel_for_thread; with no parent
    it returns the deterministic summary and never touches the DB or the model.
    Default it to "" so every existing agent-delivery test exercises that path
    without a stray Postgres connect; the concierge tests below override it.
    """
    import chat.api

    monkeypatch.setattr(chat.api, "parent_channel_for_thread", lambda _t: "")


async def test_agent_reply_message_uses_conversational_when_parent(monkeypatch):
    import chat.api

    monkeypatch.setattr(chat.api, "parent_channel_for_thread", lambda _t: "chan-1")

    async def fake_reply(channel_id, summary, details="", **_kw):
        assert channel_id == "chan-1"
        assert summary == "Added a null check in parse()."
        return "Hey, I slipped in that null check you asked for."

    monkeypatch.setattr(chat.api, "conversational_agent_reply", fake_reply)

    msg = await runner._agent_reply_message("s-1", "Added a null check in parse().", "")
    assert msg == "Hey, I slipped in that null check you asked for."


async def test_agent_reply_message_falls_back_without_parent():
    # autouse fixture forces parent="" -> deterministic summary + details.
    msg = await runner._agent_reply_message("s-1", "Fixed it.", "Added a guard.")
    assert msg == "Fixed it.\n\nAdded a guard."


async def test_agent_reply_message_fails_open_on_model_error(monkeypatch):
    import chat.api

    monkeypatch.setattr(chat.api, "parent_channel_for_thread", lambda _t: "chan-1")

    async def boom(*_a, **_kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(chat.api, "conversational_agent_reply", boom)

    msg = await runner._agent_reply_message("s-1", "Fixed it.", "Added a guard.")
    assert msg == "Fixed it.\n\nAdded a guard."


async def test_delivery_message_agent_appends_url_after_conversational(monkeypatch):
    # The conversational reply replaces the raw summary/details, but the URL is
    # still appended deterministically (never routed through the model).
    import chat.api

    monkeypatch.setattr(chat.api, "parent_channel_for_thread", lambda _t: "chan-1")

    async def fake_reply(channel_id, summary, details="", **_kw):
        return "All done, opened a PR for you."

    monkeypatch.setattr(chat.api, "conversational_agent_reply", fake_reply)

    result = (
        '{"type": "pr", "summary": "Fixed the parser.", '
        '"details": "guard + test", '
        '"url": "https://github.com/jomcgi/homelab/pull/99"}'
    )
    msg = await runner._delivery_message("s-10", {"result": result})
    assert msg.startswith("All done, opened a PR for you.")
    assert "https://github.com/jomcgi/homelab/pull/99" in msg
    assert "guard + test" not in msg  # raw details replaced by the conversational reply


# ---------------------------------------------------------------------------
# _settle: fold the terminal result into the run's single live message (ADR 024)
# ---------------------------------------------------------------------------


async def test_settle_edits_live_message_when_present(monkeypatch):
    """When the run has a live message id and the result fits one message, settle
    edits that message in place (one message) rather than posting a second."""
    fake_api = MagicMock()
    fake_api.take_progress_message = MagicMock(return_value="777")
    edits = []
    monkeypatch.setattr(
        runner,
        "_enqueue_edit_sync",
        lambda chan, msg, content: edits.append((chan, msg, content)),
    )
    deliver = AsyncMock()
    monkeypatch.setattr(runner, "_deliver", deliver)

    with patch.dict(sys.modules, {"chat.api": fake_api}):
        await runner._settle("T", "Artifact ready: https://x")

    assert edits == [("T", "777", "Artifact ready: https://x")]
    deliver.assert_not_awaited()


async def test_settle_falls_back_to_post_without_live_message(monkeypatch):
    """No live message id (MCP session, or a lost row) -> post the result as a
    new message so it is never dropped."""
    fake_api = MagicMock()
    fake_api.take_progress_message = MagicMock(return_value="")
    monkeypatch.setattr(runner, "_enqueue_edit_sync", MagicMock())
    deliver = AsyncMock()
    monkeypatch.setattr(runner, "_deliver", deliver)

    with patch.dict(sys.modules, {"chat.api": fake_api}):
        await runner._settle("T", "hi")

    deliver.assert_awaited_once_with("T", "hi")


async def test_settle_long_content_edits_first_page_and_posts_overflow(monkeypatch):
    """A result too long for one Discord message settles the live message to its
    first page and posts the overflow as follow-ups, so the live message ends on
    the result (not a stranded checklist) and no content is dropped."""
    fake_api = MagicMock()
    fake_api.take_progress_message = MagicMock(return_value="777")
    edits = []
    posts = []
    monkeypatch.setattr(
        runner,
        "_enqueue_edit_sync",
        lambda chan, msg, content: edits.append((chan, msg, content)),
    )
    monkeypatch.setattr(
        runner, "_enqueue_sync", lambda chan, content: posts.append((chan, content))
    )
    deliver = AsyncMock()
    monkeypatch.setattr(runner, "_deliver", deliver)
    long = "line\n" * 2000  # forces several Discord-sized pages

    with patch.dict(sys.modules, {"chat.api": fake_api}):
        await runner._settle("T", long)

    # First page edits the live message; the remaining pages post as follow-ups.
    assert len(edits) == 1 and edits[0][0] == "T" and edits[0][1] == "777"
    assert len(posts) >= 1
    deliver.assert_not_awaited()


async def test_settle_no_discord_thread_is_noop(monkeypatch):
    """A run with no Discord thread (pure MCP) settles nothing."""
    deliver = AsyncMock()
    monkeypatch.setattr(runner, "_deliver", deliver)
    await runner._settle("", "hi")
    deliver.assert_not_awaited()
