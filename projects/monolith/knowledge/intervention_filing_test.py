"""Persistence, rollback and concurrent duplicate tests for intervention filing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

from auth.principal import Authority, Principal, PrincipalKind
from core.clock import as_utc
from knowledge.interventions import create_intervention
from knowledge.mcp import _report_distress_sync, report_distress
from knowledge.models import Intervention, RawInput


@pytest.fixture(name="db")
def db_fixture(tmp_path, monkeypatch):
    """Use a file database so transactions behave like the deployed database."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'interventions.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    uploads: dict[str, str] = {}
    monkeypatch.setattr(
        "knowledge.raw_write.upload_raw",
        lambda raw_id, content: uploads.__setitem__(raw_id, content),
    )
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    CREATE TABLE routine_jobs (
                        name TEXT PRIMARY KEY,
                        routine_kind TEXT NOT NULL,
                        interval_secs INTEGER,
                        next_run_at TIMESTAMP,
                        payload TEXT,
                        created_by TEXT
                    )
                    """
                )
            )
            session.commit()
        yield SimpleNamespace(engine=engine, uploads=uploads)
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


@pytest.fixture(name="operator")
def operator_fixture():
    return Principal(
        subject="human:reviewer",
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )


def _reporter() -> dict[str, str]:
    return {
        "reporter_subject": "agent:sender",
        "reporter_authority": "delegated",
        "reporter_kind": "workload",
        "reporter_session": "session-1",
    }


def _raw_input(session, raw_id):
    return session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).one_or_none()


def _raw(db, content: str = "distress body") -> str:
    from knowledge.api import ingest_raw_with_status

    with Session(db.engine) as session:
        raw, _ = ingest_raw_with_status(
            session, content=content, source="distress", extra=_reporter()
        )
        create_intervention(session, raw.raw_id)
        session.commit()
        return raw.raw_id


def test_distress_commit_is_atomic_and_stamps_server_provenance(db):
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    with patch("knowledge.mcp.get_engine", return_value=db.engine):
        result, _, _, won = _report_distress_sync(
            "cannot continue", "urgent", "details", "help", _reporter()
        )

    assert won is True
    with Session(db.engine) as session:
        raw = _raw_input(session, result["intervention_id"])
        row = session.get(Intervention, result["intervention_id"])
        assert raw is not None and row is not None
        assert raw.source == "distress"
        assert raw.extra["reporter_subject"] == "agent:sender"
        for key, value in _reporter().items():
            assert raw.extra[key] == value
        assert row.state == "open"
        # SQLite returns naive UTC timestamps; PostgreSQL retains the zone.
        assert before <= as_utc(row.created_at) <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_duplicate_delivery_has_one_winner_and_preserves_ack(db, operator):
    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=operator),
        patch("knowledge.mcp._notify", notify),
    ):
        first = await report_distress("same content", "blocked")
        with Session(db.engine) as session:
            row = session.get(Intervention, first["intervention_id"])
            row.state = "acknowledged"
            row.responder_subject = "human:reviewer"
            row.acknowledged_by_subject = "human:reviewer"
            row.acknowledged_at = datetime.now(timezone.utc)
            row.acknowledged_request_revision = 1
            row.revision = 2
            session.commit()
            acknowledged_at = row.acknowledged_at
        second = await report_distress("same content", "blocked")

    assert first["status"] == "notified"
    assert second["status"] == "recorded"
    assert notify.await_count == 1
    with Session(db.engine) as session:
        assert len(session.exec(select(Intervention)).all()) == 1
        assert (
            session.get(Intervention, first["intervention_id"]).state == "acknowledged"
        )
        row = session.get(Intervention, first["intervention_id"])
        assert row.responder_subject == "human:reviewer"
        assert row.acknowledged_by_subject == "human:reviewer"
        assert row.acknowledged_at == acknowledged_at
        assert row.acknowledged_request_revision == 1
        assert row.revision == 2


def test_creator_sql_is_raw_id_only_and_never_updates(db):
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "knowledge.interventions" in statement or "interventions" in statement:
            statements.append(statement.upper())

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        with Session(db.engine) as session:
            raw_id = _raw(db, "grant test")
            assert create_intervention(session, raw_id) is False
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    inserts = [statement for statement in statements if "INSERT" in statement]
    assert inserts
    assert "(RAW_ID)" in inserts[0]
    assert not any("UPDATE" in statement for statement in statements)


def test_distress_inbox_failure_rolls_back_raw_in_the_same_transaction(db, monkeypatch):
    # Match PostgreSQL's outer transaction semantics for SQLite's legacy driver,
    # which otherwise releases a first SAVEPOINT without issuing an outer BEGIN.
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    event.listen(db.engine, "begin", begin_transaction)

    def fail_inbox(*_):
        raise RuntimeError("injected inbox failure")

    monkeypatch.setattr("knowledge.mcp.create_intervention", fail_inbox)
    try:
        with patch("knowledge.mcp.get_engine", return_value=db.engine):
            with pytest.raises(RuntimeError, match="inbox failure"):
                _report_distress_sync(
                    "blocked", "urgent", "details", "help", _reporter()
                )
        with Session(db.engine) as session:
            assert session.exec(select(RawInput)).all() == []
            assert session.exec(select(Intervention)).all() == []
    finally:
        event.remove(db.engine, "begin", begin_transaction)


@pytest.mark.asyncio
async def test_concurrent_distress_delivery_notifies_only_insert_winner(db, operator):
    import asyncio

    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=operator),
        patch("knowledge.mcp._notify", notify),
    ):
        results = await asyncio.gather(
            report_distress("simultaneous", "blocked"),
            report_distress("simultaneous", "blocked"),
        )
    assert sorted(result["status"] for result in results) == ["notified", "recorded"]
    assert results[0]["intervention_id"] == results[1]["intervention_id"]
    assert notify.await_count == 1
    with Session(db.engine) as session:
        assert len(session.exec(select(Intervention)).all()) == 1
        assert len(session.exec(select(RawInput)).all()) == 1
        assert session.execute(text("SELECT name FROM routine_jobs")).all() == []
