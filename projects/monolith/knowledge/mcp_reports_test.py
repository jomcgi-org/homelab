"""Tests for the intentional knowledge reporting MCP tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from auth.principal import Authority, Principal, PrincipalKind, anonymous_principal
from knowledge.mcp import dispute_fact, report_distress, report_knowledge
from knowledge.models import Chunk, Dispute, Note, RawInput
from knowledge.store import KnowledgeStore


@pytest.fixture(name="db")
def db_fixture(tmp_path, monkeypatch):
    """Provide a file-backed SQLite database and an in-memory raw object store."""
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-reports.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    uploads: dict[str, str] = {}
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
        monkeypatch.setattr(
            "knowledge.ingest_queue.upload_raw",
            lambda raw_id, content: uploads.__setitem__(raw_id, content),
        )
        yield SimpleNamespace(engine=engine, uploads=uploads)
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="principal")
def principal_fixture():
    return Principal(
        subject="agent:reviewer",
        actor=(),
        scope=(),
        groups=(),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.DELEGATED,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        ("repo", "repo:jomcgi-org/homelab"),
        ("org", "org:jomcgi-org"),
        ("environment", "environment:homelab"),
        ("personal", "personal:agent:reviewer"),
        (
            "session",
            f"session:agent:reviewer:{datetime.now(timezone.utc).date().isoformat()}",
        ),
    ],
)
async def test_report_knowledge_resolves_each_scope(db, principal, proposed, expected):
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
    ):
        result = await report_knowledge("The checkout supports this", proposed)

    assert result["scope"] == expected
    with Session(db.engine) as session:
        raw = session.exec(
            select(RawInput).where(RawInput.raw_id == result["raw_id"])
        ).one()
        assert raw.extra["scope"] == expected


@pytest.mark.asyncio
async def test_report_knowledge_writes_raw_and_enqueues_once(db, principal):
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
    ):
        result = await report_knowledge(
            "A grounded assertion",
            evidence=["projects/monolith/knowledge/mcp.py"],
            validity_hint="while this branch is checked out",
        )

    assert result["created"] is True
    assert result["status"] == "queued"
    with Session(db.engine) as session:
        raw = session.exec(
            select(RawInput).where(RawInput.raw_id == result["raw_id"])
        ).one()
        jobs = session.execute(text("SELECT * FROM routine_jobs")).all()
        assert raw.source == "agent-report"
        assert raw.original_path is None
        assert raw.extra["reporter_subject"] == "agent:reviewer"
        assert raw.extra["reporter_authority"] == "delegated"
        assert raw.extra["reporter_kind"] == "workload"
        assert len(jobs) == 1
        assert jobs[0].name == f"kg:{raw.raw_id}"
        assert json.loads(jobs[0].payload) == {"raw_id": raw.raw_id}
        assert "## Evidence" in db.uploads[raw.raw_id]


@pytest.mark.asyncio
async def test_report_knowledge_marks_existing_raw_duplicate(db, principal):
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
    ):
        first = await report_knowledge("A duplicate assertion")
        second = await report_knowledge("A duplicate assertion")

    assert first["status"] == "queued"
    assert first["created"] is True
    assert second["status"] == "duplicate"
    assert second["created"] is False
    assert second["raw_id"] == first["raw_id"]


@pytest.mark.asyncio
async def test_report_knowledge_validates_assertion_and_scope():
    assert await report_knowledge("   ") == {"error": "assertion must not be empty"}
    result = await report_knowledge("claim", proposed_scope="planet")
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"assertion": "a" * 20_001}, "assertion"),
        ({"assertion": "claim", "evidence": ["source"] * 21}, "evidence"),
        ({"assertion": "claim", "evidence": ["s" * 501]}, "evidence"),
        ({"assertion": "claim", "evidence": ["s" * 500] * 20}, "evidence"),
        ({"assertion": "claim", "validity_hint": "v" * 201}, "validity_hint"),
    ],
)
async def test_report_knowledge_rejects_over_cap_inputs(kwargs, field):
    result = await report_knowledge(**kwargs)
    assert field in result["error"]


@pytest.mark.asyncio
async def test_report_distress_rejects_over_cap_details():
    result = await report_distress("summary", "blocked", details="d" * 8_001)
    assert "details" in result["error"]


@pytest.mark.asyncio
async def test_dispute_fact_rejects_empty_reason():
    assert await dispute_fact("fact", " \n ") == {"error": "reason must not be empty"}


@pytest.mark.asyncio
async def test_dispute_fact_writes_row_raw_and_marks_search_result(db, principal):
    with Session(db.engine) as session:
        note = Note(
            note_id="existing-fact",
            path="existing-fact.md",
            title="Existing fact",
            content_hash="note-hash",
            content="Current body",
            type="fact",
            verification_state="verified",
            created_at=datetime.now(timezone.utc),
        )
        session.add(note)
        session.commit()
        session.refresh(note)
        chunk = Chunk(
            note_fk=note.id,
            chunk_index=0,
            section_header="",
            chunk_text="Current body",
            embedding=[0.0] * 1024,
        )
        session.add(chunk)
        session.commit()
        session.refresh(chunk)
        note_row_id = note.id
        chunk_row_id = chunk.id

    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
    ):
        result = await dispute_fact(
            "existing-fact", "Contradicted by the checkout", ["source line 12"]
        )

    assert result["status"] == "disputed"
    with Session(db.engine) as session:
        dispute = session.exec(
            select(Dispute).where(Dispute.id == result["dispute_id"])
        ).one()
        raw = session.exec(
            select(RawInput).where(RawInput.raw_id == result["raw_id"])
        ).one()
        note = session.exec(select(Note).where(Note.note_id == "existing-fact")).one()
        jobs = session.execute(text("SELECT * FROM routine_jobs")).all()
        assert dispute.note_id == "existing-fact"
        assert dispute.raw_id == raw.raw_id
        assert dispute.state == "open"
        assert dispute.reporter_subject == "agent:reviewer"
        assert dispute.reporter_authority == "delegated"
        assert dispute.evidence == ["source line 12"]
        assert raw.source == "dispute"
        assert raw.extra["note_id"] == dispute.note_id
        assert note.content == "Current body"
        assert note.verification_state == "verified"
        assert len(jobs) == 1
        assert "> Existing fact" in db.uploads[raw.raw_id]
        assert "> Current body" in db.uploads[raw.raw_id]

        with patch(
            "knowledge.store._rank_search_chunks",
            return_value=[(note_row_id, chunk_row_id, 0.9)],
        ):
            results = KnowledgeStore(session).search_notes_with_context([0.0] * 1024)
        assert results[0]["disputed"] is True


@pytest.mark.asyncio
async def test_dispute_fact_rejects_unknown_fact(db, principal):
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
    ):
        result = await dispute_fact("missing", "not true")

    assert result == {"error": "unknown fact"}
    with Session(db.engine) as session:
        assert session.exec(select(Dispute)).all() == []
        assert session.exec(select(RawInput)).all() == []


@pytest.mark.asyncio
async def test_dispute_fact_caps_reason(db, principal):
    with Session(db.engine) as session:
        session.add(
            Note(
                note_id="long-reason-fact",
                path="long-reason-fact.md",
                title="Long reason fact",
                content_hash="long-reason-hash",
                content="Current body",
                type="fact",
            )
        )
        session.commit()

    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
    ):
        result = await dispute_fact("long-reason-fact", "r" * 5_000)

    with Session(db.engine) as session:
        dispute = session.get(Dispute, result["dispute_id"])
        assert dispute is not None
        assert dispute.reason == "r" * 4_000


@pytest.mark.asyncio
async def test_dispute_fact_ingest_failure_writes_no_dispute(db, principal):
    with Session(db.engine) as session:
        session.add(
            Note(
                note_id="ingest-failure-fact",
                path="ingest-failure-fact.md",
                title="Ingest failure fact",
                content_hash="ingest-failure-hash",
                content="Current body",
                type="fact",
            )
        )
        session.commit()

    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
        patch(
            "knowledge.mcp.ingest_raw_with_status",
            side_effect=RuntimeError("upload failed"),
        ),
        pytest.raises(RuntimeError, match="upload failed"),
    ):
        await dispute_fact("ingest-failure-fact", "contradicted")

    with Session(db.engine) as session:
        assert (
            session.exec(
                select(Dispute).where(Dispute.note_id == "ingest-failure-fact")
            ).all()
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("severity", "expected_level"),
    [("blocked", "warn"), ("degraded", "warn"), ("urgent", "error")],
)
async def test_distress_writes_raw_without_enqueue_and_notifies(
    db, principal, severity, expected_level
):
    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
        patch("knowledge.mcp.notify", notify),
    ):
        result = await report_distress(
            "Cannot continue", severity, "tool is unavailable", "restore access"
        )

    assert result["status"] == "notified"
    with Session(db.engine) as session:
        raw = session.exec(
            select(RawInput).where(RawInput.raw_id == result["intervention_id"])
        ).one()
        assert raw.source == "distress"
        assert raw.extra["reporter_subject"] == "agent:reviewer"
        assert session.execute(text("SELECT * FROM routine_jobs")).all() == []
    message = notify.await_args.args[0]
    assert message == (
        f"raw {result['intervention_id']} | distress({severity}) from "
        "agent:reviewer: Cannot continue | wants: restore access"
    )
    assert notify.await_args.args[1] == expected_level


@pytest.mark.asyncio
async def test_distress_notify_failure_leaves_raw_recorded(db, principal):
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
        patch("knowledge.mcp.notify", AsyncMock(side_effect=RuntimeError("offline"))),
    ):
        result = await report_distress("Cannot continue", "blocked")

    assert result["status"] == "recorded"
    with Session(db.engine) as session:
        assert session.exec(
            select(RawInput).where(RawInput.raw_id == result["intervention_id"])
        ).one()


@pytest.mark.asyncio
async def test_distress_long_summary_keeps_raw_id_in_notification(db, principal):
    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=principal),
        patch("knowledge.mcp.notify", notify),
    ):
        result = await report_distress("s" * 3_000, "urgent")

    message = notify.await_args.args[0]
    assert message.startswith(f"raw {result['intervention_id']} | ")
    assert result["intervention_id"] in message
    assert len(message) <= 1_800


@pytest.mark.asyncio
async def test_anonymous_principal_is_recorded_not_rejected(db):
    with (
        patch("knowledge.mcp.get_engine", return_value=db.engine),
        patch("knowledge.mcp.current_principal", return_value=anonymous_principal()),
    ):
        result = await report_knowledge("Anonymous observation", "personal")

    assert result["scope"] == "personal:anonymous"
    with Session(db.engine) as session:
        raw = session.exec(
            select(RawInput).where(RawInput.raw_id == result["raw_id"])
        ).one()
        assert raw.extra["reporter_subject"] == "anonymous"
        assert raw.extra["reporter_authority"] == "anonymous"
        assert raw.extra["reporter_kind"] == "human"
