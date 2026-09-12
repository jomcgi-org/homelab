"""Agent scheduling tools stay scoped to the active Discord context."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from chat.agent import ChatDeps, create_agent
from chat.models import DiscordOutbox, ScheduledTask
from chat.scheduled_tasks import drain_once


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-schedule.db'}",
        connect_args={"check_same_thread": False},
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


async def _run_tool(agent, name, args, deps):
    captured = []

    def model(messages, info):  # type: ignore[type-arg]
        for message in messages:
            for part in getattr(message, "parts", []):
                if isinstance(part, ToolReturnPart):
                    captured.append(part.content)
                    return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=name, args=args, tool_call_id="call-1")]
        )

    await agent.run("go", model=FunctionModel(model), deps=deps)
    return captured[0]


def _deps(channel="authorized-123", author="user-456"):
    return ChatDeps(
        channel_id=channel,
        author_id=author,
        store=MagicMock(),
        embed_client=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_tool_to_proactive_delivery_uses_current_channel(engine):
    agent = create_agent(base_url="http://fake:8080")
    due = datetime.now(timezone.utc) + timedelta(minutes=5)
    with patch("core.db.get_engine", return_value=engine):
        result = await _run_tool(
            agent,
            "schedule_task",
            {
                "task_kind": "reminder",
                "schedule_kind": "one_shot",
                "due_at_iso": due.isoformat(),
                "text": "check oven",
            },
            _deps(),
        )
    assert result.startswith("Scheduled task #")

    with Session(engine) as session:
        task = session.query(ScheduledTask).one()
        assert task.channel_id == "authorized-123"
        assert task.author_id == "user-456"
        task.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(task)
        session.commit()

    assert await drain_once(engine, claimant="leader-test") == 1
    with Session(engine) as session:
        outbox = session.query(DiscordOutbox).one()
        assert outbox.channel_id == "authorized-123"
        assert "<@user-456>" in outbox.content


@pytest.mark.asyncio
async def test_cron_digest_tool_validates_and_persists(engine):
    agent = create_agent(base_url="http://fake:8080")
    with patch("core.db.get_engine", return_value=engine):
        invalid = await _run_tool(
            agent,
            "schedule_task",
            {
                "task_kind": "digest",
                "schedule_kind": "cron",
                "cron_expression": "61 * * * *",
            },
            _deps(),
        )
        valid = await _run_tool(
            agent,
            "schedule_task",
            {
                "task_kind": "digest",
                "schedule_kind": "cron",
                "cron_expression": "0 9 * * 1-5",
                "digest_mode": "decisions",
            },
            _deps(),
        )
    assert "between 0 and 59" in invalid
    assert valid.startswith("Scheduled task #")
    with Session(engine) as session:
        row = session.query(ScheduledTask).one()
        assert row.cron_expression == "0 9 * * 1-5"
        assert row.payload_json == '{"mode": "decisions"}'


@pytest.mark.asyncio
async def test_tools_reject_missing_authorized_context(engine):
    agent = create_agent(base_url="http://fake:8080")
    with patch("core.db.get_engine", return_value=engine):
        result = await _run_tool(
            agent,
            "schedule_task",
            {
                "task_kind": "digest",
                "schedule_kind": "cron",
                "cron_expression": "0 9 * * *",
            },
            _deps(channel="", author=""),
        )
    assert result == "I can't manage scheduled tasks here."
    with Session(engine) as session:
        assert session.query(ScheduledTask).count() == 0
