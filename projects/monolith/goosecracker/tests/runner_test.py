"""Unit tests for goosecracker delivery, publishing, and message composition."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

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
