import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat import summarizer
from chat.changelog import ChangelogConfig
from scheduler.api import _registry

_TEST_CHANGELOG_CONFIGS = json.dumps(
    [
        {
            "name": "test",
            "channelId": "123",
            "githubRepo": "owner/repo",
            "prompt": "professional",
            "embedTitle": "Test",
            "embedColor": "0x2ECC71",
            "intervalHours": 1,
        }
    ]
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Clear the scheduler registry before and after each test."""
    _registry.clear()
    yield
    _registry.clear()


def test_on_startup_without_bot_skips_changelog():
    """Calling on_startup without bot registers only summary_generation."""
    session = MagicMock()
    llm_call = AsyncMock()

    with patch("scheduler.api.register_job") as mock_register:
        summarizer.on_startup(session, llm_call=llm_call)

    assert mock_register.call_count == 1
    call_kwargs = mock_register.call_args_list[0]
    assert call_kwargs[1]["name"] == "chat.summary_generation"


def test_on_startup_with_bot_registers_changelog_per_config():
    """Calling on_startup with bot registers summary_generation + one job per changelog config."""
    session = MagicMock()
    bot = MagicMock()
    llm_call = AsyncMock()

    with patch.dict("os.environ", {"CHANGELOG_CONFIGS": _TEST_CHANGELOG_CONFIGS}):
        with patch("scheduler.api.register_job") as mock_register:
            summarizer.on_startup(session, bot=bot, llm_call=llm_call)

    assert mock_register.call_count == 2
    names = {call[1]["name"] for call in mock_register.call_args_list}
    assert names == {"chat.summary_generation", "chat.changelog.test"}


def test_on_startup_with_bot_no_configs_registers_only_summary():
    """With bot but empty CHANGELOG_CONFIGS, only summary_generation is registered."""
    session = MagicMock()
    bot = MagicMock()
    llm_call = AsyncMock()

    with patch.dict("os.environ", {"CHANGELOG_CONFIGS": "[]"}):
        with patch("scheduler.api.register_job") as mock_register:
            summarizer.on_startup(session, bot=bot, llm_call=llm_call)

    assert mock_register.call_count == 1
    names = {call[1]["name"] for call in mock_register.call_args_list}
    assert names == {"chat.summary_generation"}


@pytest.mark.asyncio
async def test_summary_handler_calls_generate_functions():
    """The summary handler calls generate_summaries and generate_channel_summaries."""
    session = MagicMock()
    llm_call = AsyncMock()

    with patch(
        "scheduler.api.register_job",
        side_effect=lambda _s, **kw: _registry.__setitem__(kw["name"], kw["handler"]),
    ):
        summarizer.on_startup(session, llm_call=llm_call)

    handler = _registry["chat.summary_generation"]

    with (
        patch.object(
            summarizer, "generate_summaries", new_callable=AsyncMock
        ) as mock_gen,
        patch.object(
            summarizer, "generate_channel_summaries", new_callable=AsyncMock
        ) as mock_chan,
    ):
        await handler(session)

    mock_gen.assert_called_once_with(session, llm_call)
    mock_chan.assert_called_once_with(session, llm_call)


@pytest.mark.asyncio
async def test_changelog_handler_calls_run_changelog_iteration():
    """The changelog handler calls run_changelog_iteration with bot, llm_call, and config."""
    session = MagicMock()
    bot = MagicMock()
    llm_call = AsyncMock()

    with patch(
        "chat.changelog.run_changelog_iteration", new_callable=AsyncMock
    ) as mock_iter:
        with patch.dict("os.environ", {"CHANGELOG_CONFIGS": _TEST_CHANGELOG_CONFIGS}):
            with patch(
                "scheduler.api.register_job",
                side_effect=lambda _s, **kw: _registry.__setitem__(
                    kw["name"], kw["handler"]
                ),
            ):
                summarizer.on_startup(session, bot=bot, llm_call=llm_call)

        handler = _registry["chat.changelog.test"]
        await handler(session)

    mock_iter.assert_called_once()
    args, kwargs = mock_iter.call_args
    assert args[0] is bot
    assert args[1] is llm_call
    assert isinstance(args[2], ChangelogConfig)
    assert args[2].name == "test"
    assert callable(kwargs.get("store_message"))
