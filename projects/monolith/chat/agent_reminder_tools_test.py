"""Tests for the reminder agent tools (set_reminder, list_my_reminders,
cancel_reminder) via FunctionModel, mirroring agent_channel_digest_tool_test.py.

Unlike the digest tools (which fake out build_llm_caller), these exercise the
real chat.reminders CRUD end to end against an in-memory SQLite engine patched
onto core.db.get_engine -- the interesting behaviour here is the tool's own
ISO-8601 parsing, id coercion, and error-string plumbing around a real
create/list/cancel round trip, not the CRUD logic itself (already covered by
chat/reminders_test.py).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.agent import ChatDeps, create_agent
from chat.reminders import create_reminder


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the full chat schema, schema stripped so
    SQLite accepts the DDL -- mirrors chat.reminders_test's session_fixture,
    but yields the engine (not a session) since the tool code under test
    opens its own session per call via core.db.get_engine."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original:
            table.schema = original[table.name]


def _make_deps(channel_id: str = "ch1", author_id: str = "user-1") -> ChatDeps:
    return ChatDeps(
        channel_id=channel_id,
        store=MagicMock(),
        embed_client=AsyncMock(),
        author_id=author_id,
    )


async def _run_tool_capturing_return(
    agent, tool_name: str, args: dict, deps: ChatDeps
) -> str:
    captured = []

    def model_func(messages, info):  # type: ignore[type-arg]
        for msg in messages:
            if hasattr(msg, "parts"):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        captured.append(part.content)
                        return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id="call-1")]
        )

    await agent.run("go", model=FunctionModel(model_func), deps=deps)
    assert len(captured) == 1
    return captured[0]


@pytest.fixture
def future_iso():
    dt = datetime.now(timezone.utc) + timedelta(days=1)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# set_reminder -- happy path
# ---------------------------------------------------------------------------


class TestSetReminderHappyPath:
    @pytest.mark.asyncio
    async def test_creates_pending_reminder_and_confirms_absolute_time(
        self, engine, future_iso
    ):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": future_iso, "text": "stand up"},
                deps,
            )

        assert result.startswith("Reminder #")
        assert "UTC." in result

        with Session(engine) as s:
            from chat.reminders import list_pending

            pending = list_pending(s, "user-1")
        assert len(pending) == 1
        assert pending[0].content == "stand up"

    @pytest.mark.asyncio
    async def test_accepts_trailing_z_suffix(self, engine):
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        due_at_iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": due_at_iso, "text": "check the oven"},
                deps,
            )

        assert result.startswith("Reminder #")

    @pytest.mark.asyncio
    async def test_treats_naive_timestamp_as_utc(self, engine):
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        due_at_iso = dt.strftime("%Y-%m-%dT%H:%M:%S")  # no offset, no Z
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": due_at_iso, "text": "naive time"},
                deps,
            )

        assert result.startswith("Reminder #")


# ---------------------------------------------------------------------------
# set_reminder -- unparseable input never raises
# ---------------------------------------------------------------------------


class TestSetReminderUnparseableInput:
    @pytest.mark.asyncio
    async def test_returns_plain_language_error_on_garbage_string(self, engine):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": "a", "text": "a"},
                deps,
            )

        assert "couldn't understand" in result.lower()

    @pytest.mark.asyncio
    async def test_returns_plain_language_error_on_empty_string(self, engine):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": "", "text": "reminder text"},
                deps,
            )

        assert "couldn't understand" in result.lower()


# ---------------------------------------------------------------------------
# set_reminder -- CRUD error strings pass through verbatim
# ---------------------------------------------------------------------------


class TestSetReminderCrudErrorsPassThrough:
    @pytest.mark.asyncio
    async def test_past_due_at_returns_crud_error_verbatim(self, engine):
        past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": past_iso, "text": "too late"},
                deps,
            )

        assert result == "due_at must be in the future"

    @pytest.mark.asyncio
    async def test_beyond_horizon_returns_crud_error_verbatim(self, engine):
        far_iso = (datetime.now(timezone.utc) + timedelta(days=400)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": far_iso, "text": "far future"},
                deps,
            )

        assert "366" in result

    @pytest.mark.asyncio
    async def test_pending_limit_returns_crud_error_verbatim(self, engine, future_iso):
        with Session(engine) as s:
            for i in range(10):
                create_reminder(
                    s,
                    "ch1",
                    "user-1",
                    f"r{i}",
                    datetime.now(timezone.utc) + timedelta(hours=i + 1),
                )
            s.commit()

        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": future_iso, "text": "one too many"},
                deps,
            )

        assert "10" in result


# ---------------------------------------------------------------------------
# set_reminder -- no author_id
# ---------------------------------------------------------------------------


class TestSetReminderNoAuthor:
    @pytest.mark.asyncio
    async def test_returns_cant_manage_reminders_here(self, engine, future_iso):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps(author_id="")

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": future_iso, "text": "no author"},
                deps,
            )

        assert result == "I can't manage reminders here."


# ---------------------------------------------------------------------------
# set_reminder -- fails open on unexpected exception
# ---------------------------------------------------------------------------


class TestSetReminderFailsOpen:
    @pytest.mark.asyncio
    async def test_returns_failure_string_instead_of_raising(self, future_iso):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", side_effect=RuntimeError("db unreachable")):
            result = await _run_tool_capturing_return(
                agent,
                "set_reminder",
                {"due_at_iso": future_iso, "text": "boom"},
                deps,
            )

        assert result == "I couldn't set that reminder right now, try again in a bit."


# ---------------------------------------------------------------------------
# list_my_reminders
# ---------------------------------------------------------------------------


class TestListMyReminders:
    @pytest.mark.asyncio
    async def test_lists_pending_reminders_for_author(self, engine):
        with Session(engine) as s:
            create_reminder(
                s,
                "ch1",
                "user-1",
                "first",
                datetime.now(timezone.utc) + timedelta(hours=1),
            )
            create_reminder(
                s,
                "ch1",
                "user-1",
                "second",
                datetime.now(timezone.utc) + timedelta(hours=2),
            )
            s.commit()

        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent, "list_my_reminders", {}, deps
            )

        assert "first" in result
        assert "second" in result

    @pytest.mark.asyncio
    async def test_returns_no_pending_reminders_message_when_empty(self, engine):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent, "list_my_reminders", {}, deps
            )

        assert "no pending reminders" in result.lower()

    @pytest.mark.asyncio
    async def test_only_shows_authors_own_reminders(self, engine):
        with Session(engine) as s:
            create_reminder(
                s,
                "ch1",
                "user-1",
                "mine",
                datetime.now(timezone.utc) + timedelta(hours=1),
            )
            create_reminder(
                s,
                "ch1",
                "user-2",
                "theirs",
                datetime.now(timezone.utc) + timedelta(hours=1),
            )
            s.commit()

        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps(author_id="user-1")

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent, "list_my_reminders", {}, deps
            )

        assert "mine" in result
        assert "theirs" not in result

    @pytest.mark.asyncio
    async def test_returns_cant_manage_reminders_here_without_author(self, engine):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps(author_id="")

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent, "list_my_reminders", {}, deps
            )

        assert result == "I can't manage reminders here."

    @pytest.mark.asyncio
    async def test_fails_open_on_unexpected_exception(self):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", side_effect=RuntimeError("db unreachable")):
            result = await _run_tool_capturing_return(
                agent, "list_my_reminders", {}, deps
            )

        assert (
            result == "I couldn't check your reminders right now, try again in a bit."
        )


# ---------------------------------------------------------------------------
# cancel_reminder
# ---------------------------------------------------------------------------


class TestCancelReminder:
    @pytest.mark.asyncio
    async def test_cancels_own_pending_reminder(self, engine):
        with Session(engine) as s:
            row = create_reminder(
                s,
                "ch1",
                "user-1",
                "cancel me",
                datetime.now(timezone.utc) + timedelta(hours=1),
            )
            s.commit()
            reminder_id = row.id

        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "cancel_reminder",
                {"reminder_id": reminder_id},
                deps,
            )

        assert result == f"Reminder #{reminder_id} cancelled."

        with Session(engine) as s:
            from chat.models import Reminder

            refreshed = s.get(Reminder, reminder_id)
        assert refreshed.status == "cancelled"

    @pytest.mark.asyncio
    async def test_returns_failure_string_for_missing_reminder(self, engine):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "cancel_reminder",
                {"reminder_id": 999999},
                deps,
            )

        assert result == "No pending reminder #999999 found for you."

    @pytest.mark.asyncio
    async def test_returns_failure_string_for_other_authors_reminder(self, engine):
        with Session(engine) as s:
            row = create_reminder(
                s,
                "ch1",
                "user-2",
                "not yours",
                datetime.now(timezone.utc) + timedelta(hours=1),
            )
            s.commit()
            reminder_id = row.id

        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps(author_id="user-1")

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "cancel_reminder",
                {"reminder_id": reminder_id},
                deps,
            )

        assert result == f"No pending reminder #{reminder_id} found for you."

    @pytest.mark.asyncio
    async def test_returns_cant_manage_reminders_here_without_author(self, engine):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps(author_id="")

        with patch("core.db.get_engine", return_value=engine):
            result = await _run_tool_capturing_return(
                agent,
                "cancel_reminder",
                {"reminder_id": 1},
                deps,
            )

        assert result == "I can't manage reminders here."

    @pytest.mark.asyncio
    async def test_fails_open_on_unexpected_exception(self):
        agent = create_agent(base_url="http://fake:8080")
        deps = _make_deps()

        with patch("core.db.get_engine", side_effect=RuntimeError("db unreachable")):
            result = await _run_tool_capturing_return(
                agent,
                "cancel_reminder",
                {"reminder_id": 1},
                deps,
            )

        assert (
            result == "I couldn't cancel that reminder right now, try again in a bit."
        )
