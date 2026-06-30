"""Tests for BotMessageView button callbacks.

Covers the show_thinking and fact_check button handlers:
- show_thinking sends the AI reasoning text as an ephemeral private message.
- fact_check defers, disables its button (one-per-message limit), opens a
  thread anchored to the response, and streams the fact-check into it, falling
  back to an inline followup when a thread can't be created.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from pydantic_ai import PartDeltaEvent, TextPartDelta

from chat.bot import BotMessageView


def _text_delta(content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=content))


async def _async_iter(events):
    for e in events:
        yield e


def _make_interaction() -> AsyncMock:
    """Return a fully mocked Discord Interaction.

    The ``message`` is wired for the fact-check path: no existing thread,
    an editable view, and a ``create_thread`` that yields a sendable thread
    whose ``send`` returns an editable placeholder message.
    """
    interaction = AsyncMock()
    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=AsyncMock())

    placeholder = AsyncMock()
    placeholder.edit = AsyncMock()
    thread = AsyncMock()
    thread.mention = "<#555>"
    thread.send = AsyncMock(return_value=placeholder)

    interaction.message = AsyncMock()
    interaction.message.thread = None
    interaction.message.edit = AsyncMock()
    interaction.message.create_thread = AsyncMock(return_value=thread)
    return interaction


def _get_button_by_label(view: BotMessageView, label: str) -> discord.ui.Button:
    """Extract a Button from a BotMessageView by label."""
    buttons = [
        c
        for c in view.children
        if isinstance(c, discord.ui.Button) and c.label == label
    ]
    assert len(buttons) == 1, (
        f"Expected exactly one '{label}' button, got {len(buttons)}"
    )
    return buttons[0]


class TestShowThinkingCallback:
    """Unit tests for BotMessageView.show_thinking() button callback."""

    @pytest.mark.asyncio
    async def test_sends_ephemeral_with_thinking_content(self):
        """Clicking the button sends the stored thinking text as an ephemeral reply."""
        view = BotMessageView("response text", thinking_text="AI reasoning text")
        interaction = _make_interaction()

        await _get_button_by_label(view, "Show thinking").callback(interaction)

        interaction.response.send_message.assert_called_once_with(
            "AI reasoning text", ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_ephemeral_flag_is_always_true(self):
        """The ephemeral=True keyword argument must always be present."""
        view = BotMessageView("response", thinking_text="some reasoning")
        interaction = _make_interaction()

        await _get_button_by_label(view, "Show thinking").callback(interaction)

        _, kwargs = interaction.response.send_message.call_args
        assert kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_exact_thinking_text_is_sent(self):
        """The positional argument to send_message is exactly the stored thinking text."""
        thinking = "Step 1: consider X\nStep 2: conclude Y"
        view = BotMessageView("response", thinking_text=thinking)
        interaction = _make_interaction()

        await _get_button_by_label(view, "Show thinking").callback(interaction)

        args, _ = interaction.response.send_message.call_args
        assert args[0] == thinking

    @pytest.mark.asyncio
    async def test_multiline_thinking_text(self):
        """Multiline reasoning is forwarded verbatim."""
        multiline = "First thought.\n\nSecond thought.\n\nConclusion."
        view = BotMessageView("response", thinking_text=multiline)
        interaction = _make_interaction()

        await _get_button_by_label(view, "Show thinking").callback(interaction)

        interaction.response.send_message.assert_called_once_with(
            multiline, ephemeral=True
        )

    @pytest.mark.asyncio
    async def test_only_send_message_is_called(self):
        """No other response methods (defer, edit_message) are invoked."""
        view = BotMessageView("response", thinking_text="reasoning")
        interaction = _make_interaction()

        await _get_button_by_label(view, "Show thinking").callback(interaction)

        interaction.response.defer.assert_not_called()
        interaction.response.edit_message.assert_not_called()
        interaction.response.send_message.assert_called_once()


def _patch_fact_check(mock_output: str, context: str = ""):
    """Patch the streaming fact-check agent and the context lookup.

    The agent yields ``mock_output`` as a single ``run_stream_events`` text
    delta; the streamed body is the accumulated delta text.
    """
    mock_agent = MagicMock()
    mock_agent.run_stream_events = MagicMock(
        return_value=_async_iter([_text_delta(mock_output)])
    )
    return (
        patch("chat.bot._get_fact_check_agent", return_value=mock_agent),
        patch("chat.bot._get_recent_context", return_value=context),
        mock_agent,
    )


def _streamed_message(interaction) -> AsyncMock:
    """The placeholder message the fact-check streamed into (thread path)."""
    return interaction.message.create_thread.return_value.send.return_value


class TestFactCheckCallback:
    """Unit tests for BotMessageView.fact_check() button callback."""

    @pytest.fixture(autouse=True)
    def _patch_search(self):
        """Stub the mandatory pre-search so tests never hit SearXNG."""
        with patch(
            "chat.bot.search_web",
            new=AsyncMock(return_value="LIVE_SEARCH_RESULTS"),
        ) as m:
            self.search = m
            yield

    @pytest.mark.asyncio
    async def test_opens_thread_and_streams_result(self):
        """fact_check defers, opens a thread, and streams the result into it."""
        view = BotMessageView("The R-27ER has active radar homing.")
        interaction = _make_interaction()

        p1, p2, _ = _patch_fact_check(
            "Verdict: accurate. The R-27ER does use ARH at endgame."
        )
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        interaction.response.defer.assert_called_once()
        interaction.message.create_thread.assert_called_once()
        final = _streamed_message(interaction).edit.call_args.kwargs["content"]
        assert "Fact check:" in final
        assert "accurate" in final

    @pytest.mark.asyncio
    async def test_button_disabled_after_use(self):
        """The fact-check button is disabled and the view re-edited onto the message."""
        view = BotMessageView("some claim")
        interaction = _make_interaction()
        button = _get_button_by_label(view, "Get your facts STR8!")

        p1, p2, _ = _patch_fact_check("Looks right.")
        with p1, p2:
            await button.callback(interaction)

        assert button.disabled is True
        interaction.message.edit.assert_called_once()
        assert interaction.message.edit.call_args.kwargs.get("view") is view

    @pytest.mark.asyncio
    async def test_existing_thread_blocks_repeat(self):
        """A message that already has a thread is not fact-checked again."""
        view = BotMessageView("some claim")
        interaction = _make_interaction()
        interaction.message.thread = MagicMock(mention="<#999>")

        p1, p2, mock_agent = _patch_fact_check("should not run")
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        interaction.response.defer.assert_not_called()
        mock_agent.run_stream_events.assert_not_called()
        assert (
            interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
        )

    @pytest.mark.asyncio
    async def test_inline_fallback_when_thread_creation_fails(self):
        """If a thread can't be opened, the fact-check streams inline instead."""
        view = BotMessageView("some claim")
        interaction = _make_interaction()
        interaction.message.create_thread = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(), "no nested threads")
        )

        p1, p2, _ = _patch_fact_check("Inline verdict.")
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        # The placeholder posted inline is the followup.send return value; it is
        # the message the stream edits with the final body.
        interaction.followup.send.assert_any_call("\U0001f50d Fact-checking...")
        final = interaction.followup.send.return_value.edit.call_args.kwargs["content"]
        assert "Inline verdict." in final

    @pytest.mark.asyncio
    async def test_response_text_included_in_prompt(self):
        """The agent receives the bot's response text in the prompt."""
        response = "The AIM-7P is a 1970s semi-active round."
        view = BotMessageView(response)
        interaction = _make_interaction()

        p1, p2, mock_agent = _patch_fact_check(
            "Actually the AIM-7P is a modernized variant."
        )
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        assert response in mock_agent.run_stream_events.call_args[0][0]

    @pytest.mark.asyncio
    async def test_conversation_context_prepended_when_available(self):
        """When recent channel context exists it is prepended to the prompt."""
        view = BotMessageView("The Sparrow needs continuous radar lock.")
        interaction = _make_interaction()
        context = "User: are sparrows good in BVR?\nBot: They have limitations."

        p1, p2, mock_agent = _patch_fact_check("Mixed verdict.", context=context)
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        prompt = mock_agent.run_stream_events.call_args[0][0]
        assert "Recent conversation:" in prompt
        assert context in prompt
        assert "The Sparrow needs continuous radar lock." in prompt

    @pytest.mark.asyncio
    async def test_no_context_header_when_context_empty(self):
        """When context lookup returns empty string, no 'Recent conversation:' header is added."""
        view = BotMessageView("Some claim.")
        interaction = _make_interaction()

        p1, p2, mock_agent = _patch_fact_check("Looks right.", context="")
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        assert (
            "Recent conversation:" not in mock_agent.run_stream_events.call_args[0][0]
        )

    @pytest.mark.asyncio
    async def test_agent_failure_sends_ephemeral_error(self):
        """If the fact-check agent fails, an ephemeral error is sent."""
        view = BotMessageView("some bot response")
        interaction = _make_interaction()

        mock_agent = MagicMock()
        mock_agent.run_stream_events = MagicMock(side_effect=Exception("LLM timeout"))
        with (
            patch("chat.bot._get_fact_check_agent", return_value=mock_agent),
            patch("chat.bot._get_recent_context", return_value=""),
        ):
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        interaction.response.defer.assert_called_once()
        assert interaction.followup.send.call_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_long_fact_check_result_is_truncated(self):
        """Results over the Discord message limit are truncated."""
        view = BotMessageView("some response")
        interaction = _make_interaction()

        p1, p2, _ = _patch_fact_check("x" * 3000)
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        final = _streamed_message(interaction).edit.call_args.kwargs["content"]
        assert len(final) <= 2000
        assert "truncated" in final

    @pytest.mark.asyncio
    async def test_pre_search_runs_and_results_injected(self):
        """A live web search runs and its results plus today's date ground the prompt."""
        from chat.agent import today_str

        response = "Anthropic just dropped Sonnet 5, beats Opus on coding."
        view = BotMessageView(response)
        interaction = _make_interaction()

        p1, p2, mock_agent = _patch_fact_check("Verdict: real, it shipped today.")
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        # The search is seeded from the response and its results land in the prompt.
        assert self.search.call_count == 1
        assert response[:50] in self.search.call_args[0][0]
        prompt = mock_agent.run_stream_events.call_args[0][0]
        assert "LIVE_SEARCH_RESULTS" in prompt
        assert today_str() in prompt

    @pytest.mark.asyncio
    async def test_pre_search_failure_degrades_gracefully(self):
        """If the pre-search raises, the fact-check still runs without injected results."""
        view = BotMessageView("some claim")
        interaction = _make_interaction()
        self.search.side_effect = Exception("SearXNG down")

        p1, p2, mock_agent = _patch_fact_check("Verdict from the model's own tool.")
        with p1, p2:
            await _get_button_by_label(view, "Get your facts STR8!").callback(
                interaction
            )

        mock_agent.run_stream_events.assert_called_once()
        prompt = mock_agent.run_stream_events.call_args[0][0]
        assert "LIVE_SEARCH_RESULTS" not in prompt
        assert "some claim" in prompt
