"""Hermetic persistence and state-machine tests for intervention delivery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

from auth.principal import Authority, Principal, PrincipalKind
from auth.verifier import TokenResolver
from core.db import get_session
from knowledge.http_cache import _as_utc
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
    router,
    submit_intervention_evidence,
)
from swarm.models import SwarmDecision


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


def _raw_input(session, raw_id):
    return session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).one_or_none()


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
        assert row.state == "open"
        # SQLite returns naive UTC timestamps; PostgreSQL retains the zone.
        assert before <= _as_utc(row.created_at) <= datetime.now(timezone.utc)


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
            session.get(Intervention, first["intervention_id"]).state == "acknowledged"
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
            raw_id, InterventionAcknowledgeRequest(revision=1), session, operator
        )
        assert replay["revision"] == 2
        session.commit()

    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        before = row.model_dump()
        with pytest.raises(HTTPException) as stale:
            acknowledge_intervention(
                raw_id, InterventionAcknowledgeRequest(revision=999), session, operator
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
            workflow_id="wf-real",
            node_key="node-real",
            kind="budget",
            options=["yes"],
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
        replay = associate_intervention_decision(
            raw_id,
            InterventionDecisionRequest(decision_id=decision.id, revision=1),
            session,
            operator,
        )
        assert replay == result
        assert result["decision_state"] == "open"
        assert result["associated_by_subject"] == operator.subject
        assert result["associated_at"] is not None
        with pytest.raises(HTTPException) as stale:
            associate_intervention_decision(
                raw_id,
                InterventionDecisionRequest(decision_id=decision.id, revision=999),
                session,
                operator,
            )
        assert getattr(stale.value, "status_code", None) == 409
        unchanged = session.get(SwarmDecision, decision.id)
        assert unchanged.workflow_id == "wf-real"
        assert unchanged.node_key == "node-real"
        session.commit()
        missing_raw_id = _raw(db, "missing decision")
        with pytest.raises(HTTPException) as missing:
            associate_intervention_decision(
                missing_raw_id,
                InterventionDecisionRequest(decision_id=99999, revision=1),
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
                revision=2, disposition="resolved", resolution="fixed"
            ),
            session,
            operator,
        )
        assert replay["revision"] == 3
        with pytest.raises(HTTPException) as conflict:
            resolve_intervention(
                raw_id,
                InterventionResolveRequest(
                    revision=999, disposition="no_action", resolution="changed"
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
        evidence = _raw_input(session, first["raw_id"])
        original = _raw_input(session, raw_id)
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


def _resolve(db, raw_id, operator):
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


def _decisions(db):
    with Session(db.engine) as session:
        for number in (1, 2):
            session.add(
                SwarmDecision(
                    id=number,
                    workflow_id=f"workflow-{number}",
                    node_key=f"node-{number}",
                    kind="budget",
                    options=["yes"],
                )
            )
        session.commit()


@pytest.mark.parametrize(
    "operation", ["acknowledge", "decision", "resolve", "evidence"]
)
def test_concurrent_last_revision_writers_have_one_winner(db, operator, operation):
    raw_id = _raw(db)
    if operation == "decision":
        _decisions(db)
    if operation == "resolve":
        with Session(db.engine) as session:
            acknowledge_intervention(
                raw_id, InterventionAcknowledgeRequest(revision=1), session, operator
            )
    if operation == "evidence":
        _resolve(db, raw_id, operator)
    barrier = Barrier(2)

    def attempt(number):
        with Session(db.engine, expire_on_commit=False) as session:
            # Both identity maps hold the same previous revision before either
            # writer begins. The locked reload must reject the losing request.
            cached = session.get(Intervention, raw_id)
            original_revision = cached.revision
            session.commit()
            barrier.wait(timeout=10)
            try:
                if operation == "acknowledge":
                    result = acknowledge_intervention(
                        raw_id,
                        InterventionAcknowledgeRequest(revision=original_revision),
                        session,
                        replace(operator, subject=f"human:{number}"),
                    )
                elif operation == "decision":
                    result = associate_intervention_decision(
                        raw_id,
                        InterventionDecisionRequest(
                            revision=original_revision, decision_id=number
                        ),
                        session,
                        operator,
                    )
                elif operation == "resolve":
                    result = resolve_intervention(
                        raw_id,
                        InterventionResolveRequest(
                            revision=original_revision,
                            disposition="resolved",
                            resolution=f"resolution-{number}",
                        ),
                        session,
                        operator,
                    )
                else:
                    result = submit_intervention_evidence(
                        raw_id,
                        InterventionEvidenceRequest(evidence=f"proof-{number}"),
                        session,
                        operator,
                    )
                return number, 200, result
            except HTTPException as exc:
                return number, exc.status_code, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (1, 2)))
    assert sorted(status for _, status, _ in results) == [200, 409]
    winner = next(number for number, status, _ in results if status == 200)
    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        if operation == "acknowledge":
            assert row.revision == 2
            assert row.responder_subject == f"human:{winner}"
            assert row.acknowledged_request_revision == 1
        elif operation == "decision":
            assert row.revision == 2
            assert row.decision_id == winner
            assert row.workflow_id == f"workflow-{winner}"
            assert row.node_key == f"node-{winner}"
            assert row.decision_state == "open"
            assert row.associated_by_subject == operator.subject
        elif operation == "resolve":
            assert row.revision == 3
            assert row.resolution == f"resolution-{winner}"
            assert row.resolved_request_revision == 2
        else:
            assert row.revision == 4
            assert row.evidence_by_subject == operator.subject
            assert row.evidence_submitted_at is not None
            evidence = session.exec(
                select(RawInput).where(RawInput.source == "agent-report")
            ).all()
            assert len(evidence) == 1
            assert row.evidence_raw_id == evidence[0].raw_id
            assert f"proof-{winner}" in db.uploads[row.evidence_raw_id]
            assert len(db.uploads) == 2  # The losing payload was never uploaded.


@pytest.mark.parametrize("operation", ["acknowledge", "decision", "resolve"])
@pytest.mark.parametrize(
    "conflict", ["actor", "current_revision", "arbitrary_revision"]
)
def test_replay_requires_the_accepted_actor_and_input_revision(
    db, operator, operation, conflict
):
    raw_id = _raw(db)
    if operation == "decision":
        _decisions(db)
        request = InterventionDecisionRequest(revision=1, decision_id=1)
        function = associate_intervention_decision
    elif operation == "resolve":
        with Session(db.engine) as session:
            acknowledge_intervention(
                raw_id, InterventionAcknowledgeRequest(revision=1), session, operator
            )
        request = InterventionResolveRequest(
            revision=2, disposition="resolved", resolution="fixed"
        )
        function = resolve_intervention
    else:
        request = InterventionAcknowledgeRequest(revision=1)
        function = acknowledge_intervention
    with Session(db.engine) as session:
        accepted = function(raw_id, request, session, operator)
    with Session(db.engine) as session:
        assert function(raw_id, request, session, operator) == accepted
    replay_actor = operator
    if conflict == "actor":
        replay_actor = replace(operator, subject="human:other")
    else:
        request = request.model_copy(
            update={
                "revision": accepted["revision"]
                if conflict == "current_revision"
                else 999
            }
        )
    with Session(db.engine) as session:
        with pytest.raises(HTTPException) as exc:
            function(raw_id, request, session, replay_actor)
        assert exc.value.status_code == 409
    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        assert row.revision == accepted["revision"]
        assert row.resolution == accepted["resolution"]
        assert row.decision_id == accepted["decision_id"]


def test_decision_state_is_an_exact_observation_and_replay_does_not_rewrite_it(
    db, operator
):
    raw_id = _raw(db)
    _decisions(db)
    with Session(db.engine, expire_on_commit=False) as session:
        cached = session.get(SwarmDecision, 1)
        session.commit()
        with Session(db.engine) as writer:
            row = writer.get(SwarmDecision, 1)
            row.decided_at = datetime.now(timezone.utc)
            writer.commit()
        assert cached.decided_at is None
        accepted = associate_intervention_decision(
            raw_id,
            InterventionDecisionRequest(revision=1, decision_id=1),
            session,
            operator,
        )
        assert accepted["decision_state"] == "decided"
        assert accepted["decision_id"] == 1
        assert accepted["workflow_id"] == "workflow-1"
        assert accepted["node_key"] == "node-1"
        assert accepted["associated_at"] is not None
    with Session(db.engine) as session:
        assert (
            associate_intervention_decision(
                raw_id,
                InterventionDecisionRequest(revision=1, decision_id=1),
                session,
                operator,
            )
            == accepted
        )


def test_decision_facade_identity_mismatch_cannot_associate(db, operator, monkeypatch):
    raw_id = _raw(db)
    monkeypatch.setattr(
        "knowledge.router.decision_reference",
        lambda *_: {
            "decision_id": 2,
            "state": "open",
            "workflow_id": "wrong",
            "node_key": "wrong",
        },
    )
    with Session(db.engine) as session:
        with pytest.raises(HTTPException) as exc:
            associate_intervention_decision(
                raw_id,
                InterventionDecisionRequest(revision=1, decision_id=1),
                session,
                operator,
            )
        assert exc.value.status_code == 409
        session.commit()
    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        assert row.decision_id is None and row.revision == 1


def test_evidence_attribution_is_immutable_and_survives_replay(db, operator):
    raw_id = _raw(db)
    _resolve(db, raw_id, operator)
    with Session(db.engine) as session:
        first = submit_intervention_evidence(
            raw_id, InterventionEvidenceRequest(evidence="proof"), session, operator
        )
        row = session.get(Intervention, raw_id)
        accepted = row.model_dump()
    with Session(db.engine) as session:
        with pytest.raises(HTTPException) as exc:
            submit_intervention_evidence(
                raw_id,
                InterventionEvidenceRequest(evidence="proof"),
                session,
                replace(operator, subject="human:other"),
            )
        assert exc.value.status_code == 409
    with Session(db.engine) as session:
        assert (
            submit_intervention_evidence(
                raw_id, InterventionEvidenceRequest(evidence="proof"), session, operator
            )
            == first
        )
        row = session.get(Intervention, raw_id)
        assert row.model_dump() == accepted
        assert row.evidence_by_subject == operator.subject
        assert row.evidence_submitted_at is not None
        evidence = _raw_input(session, first["raw_id"])
        assert evidence.extra["reporter_subject"] == operator.subject
        assert evidence.extra["verification_state"] == "unverified"


def test_evidence_insert_and_link_roll_back_together(db, operator):
    raw_id = _raw(db)
    _resolve(db, raw_id, operator)
    with Session(db.engine) as session:

        def reject_link(db_session, _flush_context, _instances):
            if any(
                isinstance(row, Intervention) and row.evidence_raw_id
                for row in db_session.dirty
            ):
                raise RuntimeError("injected link persistence failure")

        event.listen(session, "before_flush", reject_link)
        with pytest.raises(RuntimeError, match="link persistence"):
            submit_intervention_evidence(
                raw_id, InterventionEvidenceRequest(evidence="proof"), session, operator
            )
        event.remove(session, "before_flush", reject_link)
        session.commit()  # A caller cannot accidentally persist half the operation.
    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        assert row.evidence_raw_id is None
        assert row.evidence_by_subject is None
        assert row.evidence_submitted_at is None
        assert row.revision == 3
        assert (
            session.exec(
                select(RawInput).where(RawInput.source == "agent-report")
            ).all()
            == []
        )
        assert session.execute(text("SELECT name FROM routine_jobs")).all() == []


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


@pytest.fixture
def http(db, operator):
    principals = {
        "operator": operator,
        "delegated": replace(operator, authority=Authority.DELEGATED),
        "workload": replace(operator, kind=PrincipalKind.WORKLOAD),
        "unprivileged": replace(operator, groups=()),
    }

    class Verifier:
        async def verify(self, token):
            return principals.get(token)

    app = FastAPI()
    app.state.auth_resolver = TokenResolver([Verifier()])
    app.include_router(router)

    def sessions():
        with Session(db.engine) as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        yield client


def test_authenticated_operator_http_lifecycle_round_trip(db, operator, http):
    raw_id = _raw(db)
    _decisions(db)
    headers = {"Authorization": "Bearer operator"}
    base = f"/api/knowledge/interventions/{raw_id}"
    assert (
        http.get("/api/knowledge/interventions", headers=headers).json()[
            "interventions"
        ][0]["raw_id"]
        == raw_id
    )
    assert http.get(base, headers=headers).json()["state"] == "open"
    requests = [
        ("acknowledge", {"revision": 1}),
        ("decision", {"revision": 2, "decision_id": 1}),
        (
            "resolve",
            {"revision": 3, "disposition": "resolved", "resolution": "operator fix"},
        ),
        ("evidence", {"evidence": "validated outcome"}),
    ]
    for action, body in requests:
        response = http.post(f"{base}/{action}", headers=headers, json=body)
        assert response.status_code == 200, response.text
        replay = http.post(f"{base}/{action}", headers=headers, json=body)
        assert replay.status_code == 200 and replay.json() == response.json()
    row = http.get(base, headers=headers).json()
    assert row["revision"] == 5 and row["state"] == "resolved"
    assert row["acknowledged_by_subject"] == operator.subject
    assert row["associated_by_subject"] == operator.subject
    assert row["evidence_by_subject"] == operator.subject
    assert row["decision_state"] == "open"
    assert (
        row["acknowledged_at"] and row["associated_at"] and row["evidence_submitted_at"]
    )
    assert (
        http.post(
            f"{base}/acknowledge",
            headers=headers,
            json={"revision": 1, "responder_subject": "forged"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("token", [None, "delegated", "workload", "unprivileged"])
def test_http_denies_every_private_lifecycle_surface_without_standing_human_operator(
    db, http, token
):
    raw_id = _raw(db)
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    base = f"/api/knowledge/interventions/{raw_id}"
    for path in ("/api/knowledge/interventions", base):
        assert http.get(path, headers=headers).status_code == 403
    for action, body in (
        ("acknowledge", {"revision": 1}),
        ("decision", {"revision": 1, "decision_id": 1}),
        (
            "resolve",
            {"revision": 1, "disposition": "no_action", "resolution": "forged"},
        ),
        ("evidence", {"evidence": "forged"}),
    ):
        assert (
            http.post(f"{base}/{action}", headers=headers, json=body).status_code == 403
        )
    with Session(db.engine) as session:
        row = session.get(Intervention, raw_id)
        assert row.state == "open" and row.revision == 1
        assert row.responder_subject is None and row.decision_id is None
        assert row.resolution is None and row.evidence_raw_id is None
        assert len(session.exec(select(RawInput)).all()) == 1
