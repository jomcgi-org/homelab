"""Hermetic persistence and state-machine tests for intervention delivery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

from auth.principal import Authority, Principal, PrincipalKind
from knowledge.interventions import create_intervention
from knowledge.mcp import _report_distress_sync, report_distress
from knowledge.models import Intervention, RawInput
from knowledge.router import (
    InterventionAcknowledgeRequest,
    InterventionDecisionRequest,
    InterventionEvidenceRequest,
    InterventionResolveRequest,
    acknowledge_intervention,
    associate_intervention_decision,
    resolve_intervention,
    submit_intervention_evidence,
)
from swarm.models import SwarmDecision


@pytest.fixture(name="db")
def db_fixture(tmp_path, monkeypatch):
    """Use a file database so transactions behave like the deployed database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'interventions.db'}")
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    uploads: dict[str, str] = {}
    monkeypatch.setattr(
        "knowledge.ingest_queue.upload_raw",
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


def _raw(db, content: str = "distress body") -> str:
    from knowledge.ingest_queue import ingest_raw_with_status

    with Session(db.engine) as session:
        raw, _ = ingest_raw_with_status(
            session, content=content, source="distress", extra=_reporter()
        )
        create_intervention(session, raw.raw_id)
        session.commit()
        return raw.raw_id


def test_distress_commit_is_atomic_and_stamps_server_provenance(db):
    with patch("knowledge.mcp.get_engine", return_value=db.engine):
        result, _, _, won = _report_distress_sync(
            "cannot continue", "urgent", "details", "help", _reporter()
        )

    assert won is True
    with Session(db.engine) as session:
        raw = session.get(RawInput, result["intervention_id"])
        row = session.get(Intervention, result["intervention_id"])
        assert raw is not None and row is not None
        assert raw.source == "distress"
        assert raw.extra["reporter_subject"] == "agent:sender"
        assert row.state == "open"
        assert row.created_at.tzinfo is not None


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
            session.commit()
        second = await report_distress("same content", "blocked")

    assert first["status"] == "notified"
    assert second["status"] == "recorded"
    assert notify.await_count == 1
    with Session(db.engine) as session:
        assert len(session.exec(select(Intervention)).all()) == 1
        assert (
            session.get(Intervention, first["intervention_id"]).state
            == "acknowledged"
        )


def test_acknowledgement_records_actor_time_revision_and_rejects_conflicts(
    db, operator
):
    raw_id = _raw(db)
    with Session(db.engine) as session:
        result = acknowledge_intervention(
            raw_id, InterventionAcknowledgeRequest(revision=1), session, operator
        )
        assert result["revision"] == 2
        assert result["acknowledged_by_subject"] == operator.subject
        assert result["acknowledged_at"] is not None
        replay = acknowledge_intervention(
            raw_id, InterventionAcknowledgeRequest(revision=2), session, operator
        )
        assert replay["revision"] == 2
        session.commit()

    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        before = row.model_dump()
        with pytest.raises(HTTPException) as stale:
            acknowledge_intervention(
                raw_id, InterventionAcknowledgeRequest(revision=1), session, operator
            )
        assert getattr(stale.value, "status_code", None) == 409
        assert row.model_dump() == before

        other = Principal(
            subject="human:other",
            actor=(),
            scope=(),
            groups=("operators",),
            email=None,
            kind=PrincipalKind.HUMAN,
            authority=Authority.STANDING,
        )
        with pytest.raises(HTTPException) as competing:
            acknowledge_intervention(
                raw_id, InterventionAcknowledgeRequest(revision=1), session, other
            )
        assert competing.value.status_code == 409


def test_decision_association_is_exact_and_stale_replay_fails(db, operator):
    raw_id = _raw(db)
    with Session(db.engine) as session:
        decision = SwarmDecision(
            id=1,
            workflow_id="wf-real", node_key="node-real", kind="budget", options=["yes"]
        )
        session.add(decision)
        session.commit()
        session.refresh(decision)
        result = associate_intervention_decision(
            raw_id,
            InterventionDecisionRequest(decision_id=decision.id, revision=1),
            session,
            operator,
        )
        assert result["workflow_id"] == "wf-real"
        assert result["node_key"] == "node-real"
        assert result["revision"] == 2
        with pytest.raises(HTTPException) as stale:
            associate_intervention_decision(
                raw_id,
                InterventionDecisionRequest(decision_id=decision.id, revision=1),
                session,
                operator,
            )
        assert getattr(stale.value, "status_code", None) == 409
        unchanged = session.get(SwarmDecision, decision.id)
        assert unchanged.workflow_id == "wf-real"
        assert unchanged.node_key == "node-real"
        with pytest.raises(HTTPException) as missing:
            associate_intervention_decision(
                raw_id,
                InterventionDecisionRequest(decision_id=99999, revision=2),
                session,
                operator,
            )
        assert missing.value.status_code == 404


def test_non_open_acknowledgement_cannot_mutate(db, operator):
    raw_id = _raw(db)
    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        row.state = "resolved"
        row.revision = 4
        session.commit()
        with pytest.raises(HTTPException) as conflict:
            acknowledge_intervention(
                raw_id, InterventionAcknowledgeRequest(revision=4), session, operator
            )
        assert conflict.value.status_code == 409
        assert session.get(Intervention, raw_id).state == "resolved"


def test_resolution_requires_responder_and_terminal_state_is_immutable(db, operator):
    raw_id = _raw(db)
    with Session(db.engine) as session:
        acknowledge_intervention(
            raw_id, InterventionAcknowledgeRequest(revision=1), session, operator
        )
        resolved = resolve_intervention(
            raw_id,
            InterventionResolveRequest(
                revision=2, disposition="resolved", resolution="fixed"
            ),
            session,
            operator,
        )
        assert resolved["revision"] == 3
        replay = resolve_intervention(
            raw_id,
            InterventionResolveRequest(
                revision=3, disposition="resolved", resolution="fixed"
            ),
            session,
            operator,
        )
        assert replay["revision"] == 3
        with pytest.raises(HTTPException) as conflict:
            resolve_intervention(
                raw_id,
                InterventionResolveRequest(
                    revision=3, disposition="no_action", resolution="changed"
                ),
                session,
                operator,
            )
        assert getattr(conflict.value, "status_code", None) == 409


def test_evidence_is_deterministic_and_conflicts_roll_back(db, operator):
    raw_id = _raw(db, "original distress secret")
    with Session(db.engine) as session:
        acknowledge_intervention(
            raw_id, InterventionAcknowledgeRequest(revision=1), session, operator
        )
        resolve_intervention(
            raw_id,
            InterventionResolveRequest(
                revision=2, disposition="resolved", resolution="fixed"
            ),
            session,
            operator,
        )
        session.commit()

    with Session(db.engine) as session:
        first = submit_intervention_evidence(
            raw_id, InterventionEvidenceRequest(evidence="proof"), session, operator
        )
        session.commit()

    with Session(db.engine) as session:
        second = submit_intervention_evidence(
            raw_id, InterventionEvidenceRequest(evidence="proof"), session, operator
        )
        assert second["raw_id"] == first["raw_id"]
        session.commit()

    with Session(db.engine) as session:
        with pytest.raises(HTTPException) as conflict:
            submit_intervention_evidence(
                raw_id,
                InterventionEvidenceRequest(evidence="different"),
                session,
                operator,
            )
        assert getattr(conflict.value, "status_code", None) == 409
        session.commit()

    with Session(db.engine) as session:
        rows = session.exec(select(RawInput)).all()
        evidence = session.get(RawInput, first["raw_id"])
        original = session.get(RawInput, raw_id)
        assert len([row for row in rows if row.source == "agent-report"]) == 1
        assert evidence.extra["verification_state"] == "unverified"
        assert raw_id in db.uploads[evidence.raw_id]
        assert original.source == "distress"


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


def test_request_models_reject_caller_supplied_identity_fields():
    with pytest.raises(Exception):
        InterventionAcknowledgeRequest(revision=1, responder_subject="forged")
    with pytest.raises(Exception):
        InterventionResolveRequest(
            revision=1,
            disposition="resolved",
            resolution="done",
            actor_subject="forged",
        )
    with pytest.raises(Exception):
        InterventionEvidenceRequest(evidence="proof", subject="forged")
