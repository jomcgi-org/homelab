from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, SQLModel, create_engine

from agent_sessions import kg_feed
from agent_sessions.constants import KG_NODE_KEY, SYNTHETIC_SESSION_PREFIX
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'kg_feed_test.db'}",
        connect_args={"check_same_thread": False},
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _add_session(
    session: Session,
    name: str,
    *,
    status: str = "completed",
    age_seconds: int = kg_feed.KG_FEED_QUIET_SECONDS + 60,
    node_key: str | None = None,
    watermark: int | None = None,
    turns: int = 1,
) -> AgentSession:
    last_turn_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    row = AgentSession(
        local_session_id=name,
        workspace="<guest>",
        branch="main",
        repo="org/repo",
        status=status,
        node_key=node_key,
        last_turn_at=last_turn_at,
        kg_extracted_turn_seq=watermark,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    session.add_all(
        AgentTurn(
            session_id=row.id,
            seq=seq,
            prompt=f"prompt {seq}",
            result_text=f"result {seq}",
        )
        for seq in range(1, turns + 1)
    )
    session.commit()
    return row


def test_pick_finished_sessions_applies_all_predicates(engine):
    with Session(engine) as session:
        newest = _add_session(session, "eligible-newest", status="warn")
        newest.last_turn_at = datetime.now(timezone.utc) - timedelta(
            seconds=kg_feed.KG_FEED_QUIET_SECONDS + 1
        )
        session.add(newest)
        _add_session(session, "eligible", turns=2, watermark=1)
        pending = _add_session(session, "pending")
        session.add(
            PendingMessage(session_id=pending.id, seq=2, message_text="still running")
        )
        _add_session(session, "not-quiet", age_seconds=5)
        _add_session(session, "kg", node_key=KG_NODE_KEY)
        _add_session(session, f"{SYNTHETIC_SESSION_PREFIX}job")
        _add_session(session, "fresh-watermark", watermark=1)
        _add_session(session, "running", status="running")
        session.commit()

        picked = kg_feed.pick_finished_sessions(session, limit=20)

        assert [(row.local_session_id, seq) for row, seq in picked] == [
            ("eligible-newest", 1),
            ("eligible", 2),
        ]


def test_render_caps_turns_preserves_rationale_and_elides_middle():
    row = AgentSession(
        id=7,
        local_session_id="ember-7",
        workspace="<guest>",
        branch="feature",
        repo="org/repo",
        workflow_id="wf-7",
        node_key="worker-a",
        model="claude",
        triggered_by="dev@example.com",
        title="A" * 100,
    )
    trailer = "RATIONALE\n- path: kept.py · why: important"
    turns = [
        AgentTurn(
            session_id=7,
            seq=seq,
            prompt="p" * (kg_feed.PROMPT_CAP + 100),
            result_text="r" * (kg_feed.RESULT_CAP + 100) + "\n\n" + trailer,
            commit_sha="head",
            base_sha="base",
        )
        for seq in range(1, 25)
    ]

    rendered = kg_feed.render_session_raw(row, turns)

    assert len(rendered) <= kg_feed.DOCUMENT_CAP
    assert "truncated: true" in rendered
    assert "turns elided" in rendered
    assert "## Turn 1" in rendered
    assert "## Turn 24" in rendered
    assert rendered.count("## Turn ") < len(turns)
    assert "p" * kg_feed.PROMPT_CAP in rendered
    assert "p" * (kg_feed.PROMPT_CAP + 1) not in rendered
    assert trailer in rendered
    assert "commit: head base: base" in rendered
    assert 'scope: "repo:org/repo"' in rendered


def test_render_redacts_planted_secret_and_uses_prompt_title():
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    row = AgentSession(
        id=9,
        local_session_id="ember-9",
        workspace="<guest>",
        branch="main",
    )
    turn = AgentTurn(
        session_id=9,
        seq=4,
        prompt=f"First prompt line\nuse {secret}",
        result_text=f"token={secret}",
    )

    rendered = kg_feed.render_session_raw(row, [turn])

    assert secret not in rendered
    assert rendered.count("[REDACTED]") == 2
    assert 'title: "First prompt line"' in rendered
    assert 'scope: "session:9"' in rendered
    assert 'turn_range: "4-4"' in rendered
    assert "truncated: false" in rendered


def test_feed_once_writes_raw_advances_and_resumes_incrementally(engine, monkeypatch):
    with Session(engine) as session:
        row = _add_session(session, "incremental", turns=2)
        session_id = row.id

    monkeypatch.setenv("KG_FEED_ENABLED", "true")
    monkeypatch.setattr(kg_feed, "get_engine", lambda: engine)
    ingested = []
    enqueued = []

    def ingest(session, **kwargs):
        ingested.append(kwargs)
        return SimpleNamespace(raw_id=f"raw-{len(ingested)}"), True

    def enqueue(session, raw_id):
        enqueued.append(raw_id)
        return True

    assert asyncio.run(kg_feed.feed_once(ingest=ingest, enqueue=enqueue)) == 1
    assert len(ingested) == 1
    assert ingested[0]["source"] == "ember-session"
    assert ingested[0]["original_url"] == f"ember-session:{session_id}"
    assert ingested[0]["extra"]["turn_range"] == "1-2"
    assert "## Turn 1" in ingested[0]["content"]
    assert "## Turn 2" in ingested[0]["content"]
    assert enqueued == []
    with Session(engine) as session:
        assert session.get(AgentSession, session_id).kg_extracted_turn_seq == 2

    assert asyncio.run(kg_feed.feed_once(ingest=ingest, enqueue=enqueue)) == 0
    assert len(ingested) == 1

    with Session(engine) as session:
        session.add(
            AgentTurn(
                session_id=session_id,
                seq=3,
                prompt="new prompt",
                result_text="new result",
            )
        )
        row = session.get(AgentSession, session_id)
        row.last_turn_at = datetime.now(timezone.utc) - timedelta(
            seconds=kg_feed.KG_FEED_QUIET_SECONDS + 1
        )
        session.add(row)
        session.commit()

    assert asyncio.run(kg_feed.feed_once(ingest=ingest, enqueue=enqueue)) == 1
    assert ingested[-1]["extra"]["turn_range"] == "3-3"
    assert "## Turn 3" in ingested[-1]["content"]
    assert "## Turn 2" not in ingested[-1]["content"]
    with Session(engine) as session:
        assert session.get(AgentSession, session_id).kg_extracted_turn_seq == 3


def test_existing_raw_is_reenqueued_before_watermark_advances(engine, monkeypatch):
    with Session(engine) as session:
        row = _add_session(session, "retry")
        session_id = row.id
    monkeypatch.setenv("KG_FEED_ENABLED", "yes")
    monkeypatch.setattr(kg_feed, "get_engine", lambda: engine)
    enqueued = []

    def ingest(session, **kwargs):
        return SimpleNamespace(raw_id="existing"), False

    def enqueue(session, raw_id):
        enqueued.append(raw_id)
        return True

    assert asyncio.run(kg_feed.feed_once(ingest=ingest, enqueue=enqueue)) == 1
    assert enqueued == ["existing"]
    with Session(engine) as session:
        assert session.get(AgentSession, session_id).kg_extracted_turn_seq == 1


def test_disabled_feed_does_not_touch_database(monkeypatch):
    monkeypatch.delenv("KG_FEED_ENABLED", raising=False)

    def explode():
        raise AssertionError("disabled feed must not open the database")

    monkeypatch.setattr(kg_feed, "get_engine", explode)
    assert asyncio.run(kg_feed.feed_once()) == 0


def test_start_kg_feed_loop_is_idempotent(monkeypatch):
    task = MagicMock()
    task.done.return_value = False
    created = []

    def create_task(coro):
        coro.close()
        created.append(coro)
        return task

    monkeypatch.setattr(asyncio, "create_task", create_task)
    monkeypatch.setattr(kg_feed, "_kg_feed_task", None)

    assert kg_feed.start_kg_feed_loop() == [task]
    assert kg_feed.start_kg_feed_loop() == [task]
    assert len(created) == 1
    task.add_done_callback.assert_called_once_with(kg_feed.log_task_exception)
