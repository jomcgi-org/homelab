"""Focused Discord trigger wiring and action dispatch regression tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat import safeguards
from chat.bot import AgentFlowOutcome, ChatBot
from chat.triggers import TriggerClaim


def _message(*, bot: bool = False):
    message = MagicMock()
    message.id = 10
    message.content = "deploy failed"
    message.author.id = 200
    message.author.bot = bot
    message.author.mention = "<@200>"
    message.channel.id = 100
    message.guild = None
    message.mentions = []
    message.reference = None
    message.attachments = []
    message.embeds = []
    message.reply = AsyncMock()
    return message


def _claim(action_type: str, action_config: dict) -> TriggerClaim:
    return TriggerClaim(
        id=1,
        name="deploy",
        action_type=action_type,
        action_config=action_config,
        created_by_user_id="200",
    )


@pytest.mark.asyncio
async def test_respond_action_replies_with_expanded_template():
    bot = MagicMock()
    message = _message()
    await ChatBot.fire_trigger(
        bot,
        _claim("respond", {"content": "Saw {content} from {author}"}),
        message,
    )
    message.reply.assert_awaited_once_with("Saw deploy failed from <@200>")


@pytest.mark.asyncio
async def test_crosspost_action_sends_to_target_channel():
    bot = MagicMock()
    target = MagicMock()
    target.send = AsyncMock()
    bot.get_channel.return_value = target
    message = _message()
    await ChatBot.fire_trigger(
        bot,
        _claim(
            "crosspost",
            {"target_channel_id": "300", "content": "Alert: {content}"},
        ),
        message,
    )
    bot.get_channel.assert_called_once_with(300)
    target.send.assert_awaited_once_with("Alert: deploy failed")


@pytest.mark.asyncio
async def test_agent_run_action_uses_owner_authority_and_starts_thread():
    class FakeTextChannel:
        id = 100

    bot = MagicMock()
    bot.start_agent_flow = AsyncMock(
        return_value=AgentFlowOutcome(thread=SimpleNamespace(id=400))
    )
    message = _message()
    message.channel = FakeTextChannel()
    with (
        patch("chat.bot.discord.TextChannel", FakeTextChannel),
        patch("chat.bot.acl.is_owner", return_value=True),
    ):
        await ChatBot.fire_trigger(
            bot,
            _claim(
                "agent_run",
                {
                    "prompt": "Investigate {content}",
                    "repo": "org/repo",
                    "model": "luna",
                },
            ),
            message,
        )

    args, kwargs = bot.start_agent_flow.await_args
    assert args[0] is message.channel
    assert args[1].id == 200
    assert args[2] == "Investigate deploy failed"
    assert args[3] == "org/repo"
    assert kwargs["trigger_message"] is message
    assert kwargs["route_via_orchestrator"] is False


@pytest.mark.asyncio
async def test_agent_run_rechecks_owner_at_dispatch_time():
    bot = MagicMock()
    with (
        patch("chat.bot.acl.is_owner", return_value=False),
        pytest.raises(PermissionError),
    ):
        await ChatBot.fire_trigger(
            bot,
            _claim("agent_run", {"prompt": "go", "repo": "", "model": "luna"}),
            _message(),
        )


@pytest.mark.asyncio
async def test_evaluate_triggers_skips_bot_feedback():
    bot = MagicMock()
    with patch("chat.bot.triggers.claim_matching_for_message") as claim_matching:
        await ChatBot.evaluate_triggers(bot, _message(bot=True))
    claim_matching.assert_not_called()


@pytest.mark.asyncio
async def test_per_trigger_dispatch_failure_does_not_block_later_action():
    bot = MagicMock()
    bot.fire_trigger = AsyncMock(side_effect=[RuntimeError("bad target"), None])
    claims = [
        _claim("respond", {"content": "first"}),
        TriggerClaim(
            id=2,
            name="second",
            action_type="respond",
            action_config={"content": "second"},
            created_by_user_id="200",
        ),
    ]
    with patch("chat.bot.triggers.claim_matching_for_message", return_value=claims):
        await ChatBot.evaluate_triggers(bot, _message())
    assert bot.fire_trigger.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("author_is_bot, expected_calls", [(False, 1), (True, 0)])
async def test_on_message_wires_humans_but_not_bots_to_triggers(
    author_is_bot, expected_calls
):
    bot = MagicMock()
    bot.user.id = 999
    bot._safeguards_tasks = set()
    bot._resolve_ambient = AsyncMock(return_value=False)
    bot._process_message = AsyncMock()
    bot.evaluate_triggers = AsyncMock()
    message = _message(bot=author_is_bot)

    store = MagicMock()
    store.acquire_lock.return_value = True
    session_cm = MagicMock()
    session_cm.__enter__.return_value = MagicMock()
    session_cm.__exit__.return_value = False
    verdict = safeguards.Verdict(addressed=False)
    with (
        patch("chat.bot.Session", return_value=session_cm),
        patch("chat.bot.MessageStore", return_value=store),
        patch("chat.bot.get_engine"),
        patch("chat.bot.safeguards.observe_message", return_value=verdict),
    ):
        await ChatBot.on_message(bot, message)

    assert bot.evaluate_triggers.await_count == expected_calls
    bot._process_message.assert_awaited_once_with(message)
