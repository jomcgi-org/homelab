"""Unit tests for the goosecracker delivery/publish path (ADR 024) and WS2/WS3.

Covers the artifact publish + Discord message shaping, the default mirror wiring
(WS2: GOOSECRACKER_GIT_MIRROR defaults gitMirror when caller omits it), and the
recorded-ref line in delivery messages (WS3). Also covers _drain_agent_queue,
the conversational-queue drain added for /agent thread continuations.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

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
    # A question (no goose-result block): the answer is goose's trailing
    # narrative, so post that (banner stripped), not the head of the transcript.
    result = (
        "Loading recipe: Agent\n"
        "  __( O)>  goose is ready\n"
        "  ▸ shell git log --oneline\n"
        "abc123 some commit\n"
        "Joe has been working on the git mirror and the /agent command this week."
    )
    msg = await runner._delivery_message("s-9", "agent", {"result": result})
    assert "Joe has been working on the git mirror" in msg
    assert "Loading recipe" not in msg  # recipe banner stripped


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


def test_effective_mirror_defaults_to_env_when_caller_omits(monkeypatch):
    """GOOSECRACKER_GIT_MIRROR is injected as the base; /homelab is appended."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("", "")
    assert m == "git://mirror:9418/homelab"
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
    """repo='loom' -> gitMirror <base>/loom, not <base>/homelab."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("", "", "loom")
    assert m == "git://mirror:9418/loom"
    assert r == "main"


def test_effective_mirror_empty_repo_falls_back_to_homelab(monkeypatch):
    """repo='' -> gitMirror <base>/homelab (same as 2-arg default behavior)."""
    monkeypatch.setattr(runner, "GOOSECRACKER_GIT_MIRROR", "git://mirror:9418")
    m, r = runner._effective_mirror_ref("", "", "")
    assert m == "git://mirror:9418/homelab"
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
# _drain_agent_queue: conversational-queue drain
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with schemas stripped for SQLite compatibility."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _insert_session(engine, thread_id: str, *, running: bool, pending: str) -> None:
    from chat.models import GoosecrackerSession

    with Session(engine) as session:
        session.add(
            GoosecrackerSession(
                discord_thread=thread_id,
                recipe="agent",
                tier="",
                repo="homelab",
                transcript="do the thing",
                running=running,
                pending=pending,
            )
        )
        session.commit()


def test_drain_agent_queue_returns_task_and_clears_pending(engine):
    """When pending is non-empty, _drain_agent_queue returns the task, clears
    pending, and leaves running=True so the caller can dispatch the next turn."""
    _insert_session(engine, "d-t1", running=True, pending="do something extra")

    with patch("goosecracker.runner.get_engine", return_value=engine):
        task = runner._drain_agent_queue("d-t1")

    assert task == "do something extra"
    from chat.models import GoosecrackerSession

    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "d-t1")
    assert row.pending == ""
    assert row.running is True  # caller handles dispatching next turn


def test_drain_agent_queue_clears_running_when_empty(engine):
    """When pending is empty, _drain_agent_queue returns None and sets
    running=False so the thread accepts new replies."""
    _insert_session(engine, "d-t2", running=True, pending="")

    with patch("goosecracker.runner.get_engine", return_value=engine):
        task = runner._drain_agent_queue("d-t2")

    assert task is None
    from chat.models import GoosecrackerSession

    with Session(engine) as session:
        row = session.get(GoosecrackerSession, "d-t2")
    assert row.running is False


def test_drain_agent_queue_returns_none_for_unknown_thread(engine):
    """When no GoosecrackerSession exists for the thread, return None gracefully."""
    with patch("goosecracker.runner.get_engine", return_value=engine):
        task = runner._drain_agent_queue("no-such-thread")
    assert task is None
