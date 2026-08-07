"""Tests for the /agent prompt echo (_format_agent_prompt_echo in chat.bot).

The prompt otherwise survives only in the ~90-char thread title, so the echo
posts it in full. Covers attribution, code-fence escaping (the prompt is
user-controlled and must not break out of the block), and the 2000-char cap.

Also covers the durable agent-session path: the prompt echo and planning
placeholder remain separate from session creation.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from chat import orchestrator
from chat.bot import ChatBot, _PROMPT_ECHO_MAX, _format_agent_prompt_echo

_USER = SimpleNamespace(mention="<@123>")


def test_attributes_and_fences_the_prompt():
    out = _format_agent_prompt_echo(_USER, "add a health check endpoint")
    assert out.startswith("Prompt from <@123>:\n")
    assert "```\nadd a health check endpoint\n```" in out


def test_neutralizes_inner_code_fence():
    # A triple-backtick in the prompt must not close the echo's own fence. After
    # escaping, the only raw ``` left are the echo's own opening and closing pair.
    out = _format_agent_prompt_echo(_USER, "```rm -rf /```")
    assert out.count("```") == 2
    zwsp = chr(0x200B)
    assert f"`{zwsp}`{zwsp}`" in out  # backticks woven with a zero-width space


def test_caps_to_one_discord_message():
    out = _format_agent_prompt_echo(_USER, "x" * 5000)
    assert len(out) <= _PROMPT_ECHO_MAX
    assert out.endswith("```")
    assert "…" in out  # truncation marker


def test_short_prompt_not_truncated():
    out = _format_agent_prompt_echo(_USER, "tiny")
    assert "…" not in out
    assert out.count("tiny") == 1


def _make_bot() -> ChatBot:
    """Build a ChatBot with mocked internals, same pattern as bot_streaming_test.py."""
    with (
        patch("chat.bot.EmbeddingClient") as mock_ec,
        patch("chat.bot.create_agent") as mock_ca,
    ):
        mock_ec.return_value = AsyncMock()
        mock_ca.return_value = MagicMock()
        return ChatBot()


@pytest.mark.asyncio
async def test_handle_agent_command_starts_session_after_prompt_echo():
    """The owner starts an EmberVM session after the prompt echo and intro."""
    bot = _make_bot()
    bot._start_goosecracker_stream = MagicMock()

    interaction = MagicMock()
    interaction.guild_id = 555
    interaction.user.id = 42
    interaction.user.mention = "<@42>"
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    channel = MagicMock(spec=discord.TextChannel)
    interaction.channel = channel

    thread = MagicMock()
    thread.id = 777
    thread.mention = "<#777>"
    echo_msg = MagicMock(name="echo_msg")
    thread.send = AsyncMock(side_effect=[echo_msg, MagicMock(name="intro_msg")])
    channel.create_thread = AsyncMock(return_value=thread)

    with (
        patch("chat.bot.acl.is_owner", return_value=True),
        patch.object(
            bot,
            "_orchestrator_verdict",
            AsyncMock(return_value=orchestrator.FailOpen("test")),
        ),
        patch(
            "agent_sessions.api.start_session_for_thread", new_callable=AsyncMock
        ) as start_session,
    ):
        await bot._handle_agent_command(
            interaction, "add a health check", "jomcgi/homelab"
        )

    assert thread.send.call_count == 2
    echo_call_content = thread.send.call_args_list[0][0][0]
    assert echo_call_content.startswith("Prompt from <@42>:")

    start_session.assert_awaited_once_with(
        str(thread.id), "add a health check", "jomcgi/homelab", model="luna"
    )
    bot._start_goosecracker_stream.assert_not_called()
