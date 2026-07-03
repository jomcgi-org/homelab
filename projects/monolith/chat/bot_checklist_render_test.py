"""Tests for the ADR 035 stage checklist renderer and edit-coalescing gate."""

import itertools

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from chat import goosecracker_progress as gp
from chat.bot import (
    DISCORD_MESSAGE_LIMIT,
    ChatBot,
    _should_edit_checklist,
    render_checklist,
)


def _stage(index, title, state):
    return gp.Stage(index=index, title=title, state=state)


def _progress(stages, done=False, notice=""):
    return gp.Progress(stages=stages, done=done, notice=notice)


# ---------------------------------------------------------------------------
# render_checklist
# ---------------------------------------------------------------------------


class TestRenderChecklistFallback:
    def test_none_progress_returns_none(self):
        """No snapshot yet: fall back to the tail renderer."""
        assert render_checklist(None) is None

    def test_no_stages_returns_none(self):
        """A run that never emits stage markers falls back to the tail renderer."""
        assert render_checklist(_progress(stages=[])) is None


class TestRenderChecklistStates:
    def test_each_state_gets_its_emoji(self):
        # A failed stage's one-line reason is carried in the title by the
        # marker producer, so the renderer just shows the title as-is.
        stages = [
            _stage(0, "Plan", "pending"),
            _stage(1, "Fetch data", "running"),
            _stage(2, "Write file", "done"),
            _stage(3, "Deploy (connection refused)", "failed"),
            _stage(4, "Notify", "skipped"),
        ]
        body = render_checklist(_progress(stages))

        assert "⬜ Plan" in body
        assert "🔄 Fetch data" in body
        assert "✅ Write file" in body
        assert "❌ Deploy (connection refused)" in body
        assert "⏭️ Notify" in body

    def test_header_working_when_not_done(self):
        body = render_checklist(_progress([_stage(0, "Plan", "running")]))
        assert "Working" in body

    def test_header_done_when_progress_done(self):
        body = render_checklist(_progress([_stage(0, "Plan", "done")], done=True))
        assert "Done" in body

    def test_notice_shown_while_not_done(self):
        body = render_checklist(
            _progress(
                [_stage(0, "Plan", "pending")], notice="retrying a transient failure"
            )
        )
        assert "retrying a transient failure" in body

    def test_notice_hidden_once_done(self):
        body = render_checklist(
            _progress(
                [_stage(0, "Plan", "done")], done=True, notice="stale retry notice"
            )
        )
        assert "stale retry notice" not in body

    def test_done_run_resolves_lingering_stages(self):
        # The local router model sometimes skips the trailing done marker, so a
        # completed run can still carry running/pending stages. On done the
        # renderer resolves them (running -> done, pending -> skipped, failed
        # kept) so the final edit shows every stage resolved, per the spec.
        stages = [
            _stage(0, "Fetch", "running"),
            _stage(1, "Write", "pending"),
            _stage(2, "Deploy (connection refused)", "failed"),
        ]
        body = render_checklist(_progress(stages, done=True))

        assert "✅ Fetch" in body
        assert "⏭️ Write" in body
        assert "❌ Deploy (connection refused)" in body
        assert "🔄" not in body  # nothing still spinning after the run ended

    def test_live_run_keeps_running_and_pending(self):
        # The coercion applies only once done: a live run shows real states.
        body = render_checklist(
            _progress([_stage(0, "Fetch", "running"), _stage(1, "Write", "pending")])
        )
        assert "🔄 Fetch" in body
        assert "⬜ Write" in body


class TestRenderChecklistIsTimeFree:
    def test_identical_progress_renders_identically(self):
        """No wall-clock component: same stage state -> byte-identical output.

        This is what lets the stream loop gate edits on stages_version alone.
        """
        stages = [_stage(0, "Plan", "running")]
        first = render_checklist(_progress(stages))
        second = render_checklist(_progress(stages))
        assert first == second


class TestRenderChecklistCollapse:
    def test_long_completed_run_collapses_to_earlier_stages_line(self):
        # Enough long-titled done stages to blow past 1900 chars, plus an
        # active window that must survive collapsing.
        done_stages = [
            _stage(i, f"Completed step number {i} with a fairly long title", "done")
            for i in range(60)
        ]
        active_stages = [
            _stage(60, "Currently running step", "running"),
            _stage(61, "Not started yet", "pending"),
        ]
        body = render_checklist(_progress(done_stages + active_stages))

        assert "earlier stages" in body
        assert "Currently running step" in body
        assert "Not started yet" in body
        assert len(body) <= DISCORD_MESSAGE_LIMIT

    def test_never_exceeds_discord_message_limit(self):
        # Even with an enormous active window that alone can't be collapsed,
        # the hard cap must still apply.
        active_stages = [
            _stage(i, f"Running step {i} " * 20, "running") for i in range(20)
        ]
        body = render_checklist(_progress(active_stages))
        assert len(body) <= DISCORD_MESSAGE_LIMIT

    def test_never_collapses_running_pending_or_failed(self):
        stages = [_stage(i, f"Done step {i} " * 10, "done") for i in range(50)]
        stages.append(_stage(50, "Active step", "running"))
        stages.append(_stage(51, "Failed step (boom)", "failed"))
        stages.append(_stage(52, "Pending step", "pending"))
        body = render_checklist(_progress(stages))

        assert "Active step" in body
        assert "Failed step (boom)" in body
        assert "Pending step" in body


# ---------------------------------------------------------------------------
# _should_edit_checklist gating
# ---------------------------------------------------------------------------


class TestShouldEditChecklistGating:
    def test_first_change_after_min_interval_edits(self):
        assert _should_edit_checklist(
            stages_version=1,
            last_stages_version=-1,
            done=False,
            last_done=False,
            now=100.0,
            last_edit_at=0.0,
        )

    def test_no_change_does_not_edit(self):
        assert not _should_edit_checklist(
            stages_version=1,
            last_stages_version=1,
            done=False,
            last_done=False,
            now=200.0,
            last_edit_at=0.0,
        )

    def test_change_within_min_interval_is_coalesced(self):
        assert not _should_edit_checklist(
            stages_version=2,
            last_stages_version=1,
            done=False,
            last_done=False,
            now=100.5,
            last_edit_at=100.0,
        )

    def test_change_after_min_interval_edits(self):
        assert _should_edit_checklist(
            stages_version=2,
            last_stages_version=1,
            done=False,
            last_done=False,
            now=102.0,
            last_edit_at=100.0,
        )

    def test_done_flip_alone_triggers_edit(self):
        assert _should_edit_checklist(
            stages_version=1,
            last_stages_version=1,
            done=True,
            last_done=False,
            now=102.0,
            last_edit_at=100.0,
        )

    def test_rapid_version_bumps_within_window_coalesce_to_one_edit(self):
        """Two version bumps arriving within 2s of each other, and of the last
        edit, must not each trigger an edit, only the first (or a later poll
        once the window has passed) does."""
        last_stages_version = -1
        last_done = False
        last_edit_at = 0.0
        edits = 0

        # poll 1: version 0 -> 1, far past the window -> edits
        if _should_edit_checklist(
            1, last_stages_version, False, last_done, 100.0, last_edit_at
        ):
            edits += 1
            last_stages_version, last_done, last_edit_at = 1, False, 100.0

        # poll 2: version 1 -> 2, only 0.5s later -> coalesced, no edit
        if _should_edit_checklist(
            2, last_stages_version, False, last_done, 100.5, last_edit_at
        ):
            edits += 1
            last_stages_version, last_done, last_edit_at = 2, False, 100.5

        # poll 3: still version 2 (no new bump), only 0.9s after the last edit -> no edit
        if _should_edit_checklist(
            2, last_stages_version, False, last_done, 100.9, last_edit_at
        ):
            edits += 1
            last_stages_version, last_done, last_edit_at = 2, False, 100.9

        assert edits == 1
        # The coalesced version bump (2) was never recorded as edited, proving
        # it was swallowed rather than double-counted.
        assert last_stages_version == 1


# ---------------------------------------------------------------------------
# Full loop integration: the loop renders LIVE progress only. On done it stops
# without a terminal render, because the runner settles the final result into
# the same message via a durable outbox edit (one message, not two). A terminal
# edit here would race that settle and could revert the result to a checklist.
# ---------------------------------------------------------------------------


def _make_bot() -> ChatBot:
    with (
        patch("chat.bot.EmbeddingClient") as mock_ec,
        patch("chat.bot.create_agent") as mock_ca,
    ):
        mock_ec.return_value = AsyncMock()
        mock_ca.return_value = MagicMock()
        bot = ChatBot()
    return bot


class TestStreamLoopHandsTerminalToOutbox:
    @pytest.mark.asyncio
    async def test_records_live_message_id_up_front(self):
        """The loop stamps the run's live message id so the off-loop runner can
        settle the final result into that same message."""
        bot = _make_bot()
        message = MagicMock()
        message.content = ""
        message.id = 99
        message.edit = AsyncMock()

        snap = _progress([_stage(0, "Plan", "done")], done=True)
        snap.stages_version = 1

        # A global time.monotonic patch is shared with the event loop, which the
        # up-front asyncio.to_thread(set_progress_message) drives; use an unbounded
        # clock and assert behaviour, not clock-read counts.
        with (
            patch("chat.bot.asyncio.sleep", new=AsyncMock()),
            patch("chat.bot.goosecracker.set_progress_message") as mock_set,
            patch("chat.bot.time.monotonic", side_effect=itertools.count(100.0)),
            patch("chat.bot.goosecracker_progress.get", side_effect=[snap]),
            patch("chat.bot.goosecracker_progress.clear"),
        ):
            await bot._stream_goosecracker_progress("t-7", message, kind="agent")

        mock_set.assert_called_once_with("t-7", "99")

    @pytest.mark.asyncio
    async def test_done_poll_stops_without_a_terminal_edit(self):
        """The running poll edits the live checklist; the done poll stops without
        editing, leaving the terminal settle to the runner's outbox edit."""
        bot = _make_bot()
        message = MagicMock()
        message.content = ""
        message.id = 1
        message.edit = AsyncMock()

        snap1 = _progress([_stage(0, "Plan", "running")])
        snap1.stages_version = 1
        snap2 = _progress([_stage(0, "Plan", "done")], done=True)
        snap2.stages_version = 1

        with (
            patch("chat.bot.asyncio.sleep", new=AsyncMock()),
            patch("chat.bot.goosecracker.set_progress_message"),
            patch("chat.bot.time.monotonic", side_effect=itertools.count(100.0)),
            patch("chat.bot.goosecracker_progress.get", side_effect=[snap1, snap2]),
            patch("chat.bot.goosecracker_progress.clear") as mock_clear,
        ):
            await bot._stream_goosecracker_progress(
                "artifact-1", message, kind="artifact"
            )

        # poll 1 (running) edits the live checklist; poll 2 (done) returns without
        # a terminal edit -> exactly one edit.
        assert message.edit.call_count == 1
        mock_clear.assert_called_once_with("artifact-1")

    @pytest.mark.asyncio
    async def test_edit_http_exception_does_not_crash_the_loop(self):
        bot = _make_bot()
        message = MagicMock()
        message.content = ""
        message.id = 7
        message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))

        snap1 = _progress([_stage(0, "Plan", "running")])
        snap1.stages_version = 1
        snap2 = _progress([_stage(0, "Plan", "done")], done=True)
        snap2.stages_version = 1

        with (
            patch("chat.bot.asyncio.sleep", new=AsyncMock()),
            patch("chat.bot.goosecracker.set_progress_message"),
            patch("chat.bot.time.monotonic", side_effect=itertools.count(100.0)),
            patch("chat.bot.goosecracker_progress.get", side_effect=[snap1, snap2]),
            patch("chat.bot.goosecracker_progress.clear") as mock_clear,
        ):
            await bot._stream_goosecracker_progress(
                "artifact-2", message, kind="artifact"
            )

        # The running-poll edit raised; the loop swallowed it and still reached the
        # done poll, which stops cleanly.
        message.edit.assert_awaited_once()
        mock_clear.assert_called_once_with("artifact-2")

    @pytest.mark.asyncio
    async def test_no_stages_path_edits_live_body_then_stops_on_done(self):
        """Artifact recipes that never emit markers keep the tail-renderer live
        edits while running, and likewise stop on done without a terminal edit."""
        bot = _make_bot()
        message = MagicMock()
        message.content = ""
        message.id = 3
        message.edit = AsyncMock()

        snap_thinking = gp.Progress(text="", done=False)
        snap_done = gp.Progress(text="built it", done=True)

        # The no-stages path renders via _render_goosecracker_progress, which
        # itself calls time.monotonic() (for the "flowing" window), so the count
        # of clock reads per iteration is an implementation detail. Use an
        # unbounded increasing clock and assert edit behaviour, not call counts.
        with (
            patch("chat.bot.asyncio.sleep", new=AsyncMock()),
            patch("chat.bot.goosecracker.set_progress_message"),
            patch("chat.bot.time.monotonic", side_effect=itertools.count(100.0)),
            patch(
                "chat.bot.goosecracker_progress.get",
                side_effect=[snap_thinking, snap_done],
            ),
            patch("chat.bot.goosecracker_progress.clear") as mock_clear,
        ):
            await bot._stream_goosecracker_progress(
                "artifact-3", message, kind="artifact"
            )

        # Only the running poll edits the live body; the done poll stops without a
        # terminal edit (the outbox settle delivers "built it").
        assert message.edit.call_count == 1
        mock_clear.assert_called_once_with("artifact-3")
