"""Tests for BotMessageView button callbacks.

Covers the show_thinking and fact_check button handlers:
- show_thinking sends the AI reasoning text as an ephemeral private message.
- fact_check defers the interaction, calls the fact-check agent, and sends a
  public followup with the result.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from chat.bot import BotMessageView


def _make_interaction() -> AsyncMock:
    """Return a fully mocked Discord Interaction."""
    interaction = AsyncMock()
    interaction.response = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
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
    """Patch both the fact-check agent and context lookup for button callback tests."""
    mock_result = MagicMock()
    mock_result.output = mock_output
    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value=mock_result)
    return (
        patch("chat.bot._get_fact_check_agent", return_value=mock_agent),
        patch("chat.bot._get_recent_context", return_value=context),
        mock_agent,
    )


class TestFactCheckCallback:
    """Unit tests for BotMessageView.fact_check() button callback."""

    @pytest.mark.asyncio
    async def test_defers_then_sends_followup(self):
        """fact_check defers the interaction and sends a public followup."""
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
        interaction.followup.send.assert_called_once()
        call_args = interaction.followup.send.call_args
        assert "Fact check:" in call_args[0][0]
        assert "accurate" in call_args[0][0]

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

        assert response in mock_agent.run.call_args[0][0]

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

        prompt = mock_agent.run.call_args[0][0]
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

        assert "Recent conversation:" not in mock_agent.run.call_args[0][0]

    @pytest.mark.asyncio
    async def test_agent_failure_sends_ephemeral_error(self):
        """If the fact-check agent fails, an ephemeral error is sent."""
        view = BotMessageView("some bot response")
        interaction = _make_interaction()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(side_effect=Exception("LLM timeout"))
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

        sent_content = interaction.followup.send.call_args[0][0]
        assert len(sent_content) <= 2000
        assert "truncated" in sent_content
