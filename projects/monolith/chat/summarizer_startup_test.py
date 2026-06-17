"""Tests covering the on_startup branches that were not exercised in startup_test.py:
- llm_call=None triggers build_llm_caller() internally
- the _changelog_handler returns an interval-aligned datetime
"""

import json
from datetime import datetime, timedelta, timezone
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
    """Reset scheduler registry around each test."""
    _registry.clear()
    yield
    _registry.clear()


class TestOnStartupWithNullLlmCall:
    def test_on_startup_without_llm_call_calls_build_llm_caller(self):
        """When llm_call is None, on_startup calls build_llm_caller() to create one."""
        session = MagicMock()
        fake_llm = AsyncMock()

        with (
            patch(
                "chat.summarizer.build_llm_caller", return_value=fake_llm
            ) as mock_build,
            patch("scheduler.api.register_job"),
        ):
            summarizer.on_startup(session)  # no llm_call kwarg

        mock_build.assert_called_once()

    def test_on_startup_without_llm_call_registers_summary_job(self):
        """on_startup(session) with default llm_call still registers summary_generation."""
        session = MagicMock()
        fake_llm = AsyncMock()

        with (
            patch("chat.summarizer.build_llm_caller", return_value=fake_llm),
            patch("scheduler.api.register_job") as mock_register,
        ):
            summarizer.on_startup(session)

        names = [c[1]["name"] for c in mock_register.call_args_list]
        assert "chat.summary_generation" in names

    def test_on_startup_provided_llm_call_does_not_call_build_llm_caller(self):
        """When llm_call is explicitly provided, build_llm_caller is never called."""
        session = MagicMock()
        provided_llm = AsyncMock()

        with (
            patch("chat.summarizer.build_llm_caller") as mock_build,
            patch("scheduler.api.register_job"),
        ):
            summarizer.on_startup(session, llm_call=provided_llm)

        mock_build.assert_not_called()


class TestChangelogHandlerReturnValue:
    @pytest.mark.asyncio
    async def test_changelog_handler_returns_next_interval_boundary(self):
        """The _changelog_handler returned by on_startup returns a datetime aligned to
        the next interval boundary from midnight."""
        session = MagicMock()
        bot = MagicMock()
        llm_call = AsyncMock()

        captured_handlers = {}

        def _capture_register(_s, **kw):
            captured_handlers[kw["name"]] = kw["handler"]

        with (
            patch("scheduler.api.register_job", side_effect=_capture_register),
            patch("chat.changelog.run_changelog_iteration", new_callable=AsyncMock),
            patch.dict("os.environ", {"CHANGELOG_CONFIGS": _TEST_CHANGELOG_CONFIGS}),
        ):
            summarizer.on_startup(session, bot=bot, llm_call=llm_call)

        handler = captured_handlers["chat.changelog.test"]
        result = await handler(session)

        assert result is not None
        assert isinstance(result, datetime)
        assert result.tzinfo is not None  # must be timezone-aware

        # Must be in the future (strictly after now)
        now = datetime.now(timezone.utc)
        assert result > now

        # Must be at an interval boundary (minute=0, second=0 for 1h interval)
        assert result.minute == 0
        assert result.second == 0
        assert result.microsecond == 0

    @pytest.mark.asyncio
    async def test_changelog_handler_next_boundary_at_most_one_interval_away(self):
        """The next boundary returned is within one interval of now."""
        session = MagicMock()
        bot = MagicMock()
        llm_call = AsyncMock()

        captured_handlers = {}

        def _capture_register(_s, **kw):
            captured_handlers[kw["name"]] = kw["handler"]

        with (
            patch("scheduler.api.register_job", side_effect=_capture_register),
            patch("chat.changelog.run_changelog_iteration", new_callable=AsyncMock),
            patch.dict("os.environ", {"CHANGELOG_CONFIGS": _TEST_CHANGELOG_CONFIGS}),
        ):
            summarizer.on_startup(session, bot=bot, llm_call=llm_call)

        handler = captured_handlers["chat.changelog.test"]
        now_before = datetime.now(timezone.utc)
        result = await handler(session)
        now_after = datetime.now(timezone.utc)

        # result must be within (now, now + 3600s] for a 1h interval
        assert result <= now_after + timedelta(seconds=3600)

    @pytest.mark.asyncio
    async def test_changelog_handler_calls_run_changelog_iteration(self):
        """The _changelog_handler invokes run_changelog_iteration with bot, llm_call, and config."""
        session = MagicMock()
        bot = MagicMock()
        llm_call = AsyncMock()

        captured_handlers = {}

        def _capture_register(_s, **kw):
            captured_handlers[kw["name"]] = kw["handler"]

        with (
            patch("scheduler.api.register_job", side_effect=_capture_register),
            patch(
                "chat.changelog.run_changelog_iteration", new_callable=AsyncMock
            ) as mock_iter,
            patch.dict("os.environ", {"CHANGELOG_CONFIGS": _TEST_CHANGELOG_CONFIGS}),
        ):
            summarizer.on_startup(session, bot=bot, llm_call=llm_call)
            await captured_handlers["chat.changelog.test"](session)

        mock_iter.assert_called_once()
        args, kwargs = mock_iter.call_args
        assert args[0] is bot
        assert args[1] is llm_call
        assert isinstance(args[2], ChangelogConfig)
        assert args[2].name == "test"
        assert callable(kwargs.get("store_message"))
