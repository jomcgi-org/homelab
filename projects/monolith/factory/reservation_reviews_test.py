"""Review authority is durable, bounded, and distinct from executor liveness."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import json
import pytest
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine, select

from factory import reservation_reviews as reviews
from factory.execution import review_leases as leases
from factory.execution.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentSession,
    PendingMessage,
    AgentTurn,
    AgentResultReceipt,
)
from factory.execution.review_leases import ReservationReview
from factory.orchestration.models import SwarmNodeRun, SwarmTask

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reviews.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"agent_sessions": None, "swarm": None}
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentSession,
                PendingMessage,
                AgentTurn,
                AgentResultReceipt,
                ReservationReview,
                SwarmTask,
                SwarmNodeRun,
            )
        ],
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE routine_jobs (name TEXT PRIMARY KEY,next_run_at TEXT,last_status TEXT)"
            )
        )
    monkeypatch.setattr(reviews, "get_engine", lambda: engine)
    monkeypatch.setattr(leases, "now", lambda: NOW)
    monkeypatch.setenv("FACTORY_RESERVATION_REVIEW_ENABLED", "true")
    yield engine
    engine.dispose()


def seed(db, *, age=1600, state="running", guest="guest", key="work", tier="project"):
    with Session(db) as session:
        agent = AgentSession(
            local_session_id=key,
            workspace="guest",
            branch="main",
            status="running",
            ember_session_id=guest,
            workflow_id=key,
        )
        session.add(agent)
        session.flush()
        permit = AgentCapacityReservation(
            local_session_id=key,
            pending_seq=1,
            session_id=agent.id,
            tier=tier,
            model="sol",
            owner="executor",
            state=state,
            created_at=NOW - timedelta(seconds=age),
        )
        session.add(permit)
        session.add(
            PendingMessage(
                session_id=agent.id,
                seq=1,
                message_text="Implement the feature",
                partial_text="Implemented the API; validating edge cases",
                claimed_by_replica="executor",
                dispatch_count=1,
                claimed_at=NOW,
            )
        )
        session.commit()
        session.refresh(permit)
        return permit.id


def ready(db, **kwargs):
    pid = seed(db, **kwargs)
    candidate = reviews.claim_review()
    candidate["live_guest"] = {"session_id": "guest", "state": "running"}
    return pid, candidate


def decision(action="approve"):
    return {
        "action": action,
        "reason": "Useful implementation progress",
        "guidance": "Check the schema" if action == "steer" else "",
    }


def test_review_is_due_before_30_minute_expiry(db, monkeypatch):
    pid = seed(db, age=1499)
    assert reviews.claim_review() is None
    monkeypatch.setattr(leases, "now", lambda: NOW + timedelta(seconds=1))
    candidate = reviews.claim_review()
    assert candidate["snapshot"]["permit_id"] == pid
    with Session(db) as session:
        assert session.get(ReservationReview, pid).lease_expires_at.replace(
            tzinfo=timezone.utc
        ) == NOW + timedelta(seconds=301)


def test_starting_review_or_heartbeat_does_not_renew(db, monkeypatch):
    pid, candidate = ready(db, age=1801)
    with Session(db) as session:
        pending = session.exec(select(PendingMessage)).one()
        pending.claimed_at = NOW
        session.add(pending)
        session.commit()
        assert not leases.may_dispatch(
            session, session.get(AgentCapacityReservation, pid)
        )
    assert reviews.health_snapshot()["ok"] is False


def test_completed_approval_renews_exact_dispatch(db):
    pid, candidate = ready(db, age=1801)
    reviews.apply_decision(candidate, decision())
    with Session(db) as session:
        row = session.get(ReservationReview, pid)
        assert row.state == "approved"
        assert row.lease_expires_at.replace(tzinfo=timezone.utc) == NOW + timedelta(
            minutes=30
        )
        assert leases.may_dispatch(session, session.get(AgentCapacityReservation, pid))
    assert reviews.health_snapshot()["ok"] is True


def test_changed_dispatch_cannot_receive_old_approval(db):
    pid, candidate = ready(db)
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        permit.owner = "replacement"
        session.add(permit)
        session.commit()
    reviews.apply_decision(candidate, decision())
    with Session(db) as session:
        assert session.get(ReservationReview, pid).state == "blocked"


def test_no_substantive_progress_cannot_renew_again(db, monkeypatch):
    pid, candidate = ready(db)
    reviews.apply_decision(candidate, decision())
    monkeypatch.setattr(leases, "now", lambda: NOW + timedelta(minutes=25))
    candidate = reviews.claim_review()
    candidate["live_guest"] = {"session_id": "guest", "state": "running"}
    reviews.apply_decision(candidate, decision())
    with Session(db) as session:
        assert session.get(ReservationReview, pid).state == "blocked"


def test_reserved_pre_dispatch_work_can_receive_explicit_approval(db):
    pid, candidate = ready(db, state="reserved", guest=None, age=1801)
    reviews.apply_decision(candidate, decision())
    with Session(db) as session:
        assert session.get(ReservationReview, pid).state == "approved"
        assert leases.may_dispatch(session, session.get(AgentCapacityReservation, pid))


def test_unknown_outcomes_never_receive_model_approval(db):
    seed(db, state="uncertain", age=7200)
    assert reviews.claim_review() is None
    health = reviews.health_snapshot()
    assert not health["ok"]
    assert health["overdue"][0]["review_state"] == "blocked"


def test_global_review_claim_is_atomic_before_reviewer_session_exists(db):
    seed(db, key="one")
    seed(db, key="two")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reviews.claim_review(), range(2)))
    assert sum(r is not None for r in results) == 1


def test_uncertain_reviewer_blocks_another_reviewer(db):
    seed(db)
    seed(db, key=leases.PREFIX + "lost", tier="interactive", state="uncertain")
    assert reviews.claim_review() is None
    assert not reviews.health_snapshot()["ok"]


def test_held_routine_job_cannot_disappear_from_health(db):
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO routine_jobs VALUES ('held',NULL,'invocation_outcome_unknown')"
            )
        )
    health = reviews.health_snapshot()
    assert health["held_jobs"] == 1 and not health["ok"]


def test_factory_attempt_without_permit_is_unhealthy_after_lease(db):
    with Session(db) as session:
        session.add(SwarmTask(id="task", task_text="work", conductor_model="astra"))
        session.add(
            SwarmNodeRun(
                task_id="task",
                node_key="work",
                attempt=1,
                status="admitted",
                dispatch_key="missing",
                created_at=NOW - timedelta(hours=1),
            )
        )
        session.commit()
    assert reviews.health_snapshot()["uncovered_attempts"]


def test_existing_permit_covers_factory_attempt_by_workflow(db):
    seed(db, key="workflow", age=10)
    with Session(db) as session:
        session.add(SwarmTask(id="task", task_text="work", conductor_model="astra"))
        session.add(
            SwarmNodeRun(
                task_id="task",
                node_key="work",
                attempt=1,
                status="admitted",
                dispatch_key="workflow",
                created_at=NOW - timedelta(hours=1),
            )
        )
        session.commit()
    assert reviews.health_snapshot()["ok"]


@pytest.mark.parametrize("change", ["blocked", "expired", "replacement", "progress"])
def test_stop_rechecks_authority_at_commit(db, monkeypatch, change):
    from factory.orchestration import factory_attempt_stop as stops

    pid, candidate = ready(db)
    candidate["stop_identity"] = {
        "task_id": "task",
        "node_key": "work",
        "attempt": 1,
        "identity_sha256": "a" * 64,
    }

    def stop(**kwargs):
        assert kwargs["expected_identity_sha256"] == "a" * 64
        with Session(db) as session:
            row = session.get(ReservationReview, pid)
            if change == "blocked":
                row.state = "blocked"
            if change == "replacement":
                row.review_session_key = "another-review"
            if change == "expired":
                row.requested_at = NOW - timedelta(minutes=10)
            if change == "progress":
                pending = session.exec(select(PendingMessage)).one()
                pending.partial_text = "New progress"
                session.add(pending)
            session.add(row)
            session.commit()
        with Session(db) as session:
            kwargs["authorization_check"](session)
        pytest.fail("Stale stop authority accepted")

    monkeypatch.setattr(stops, "request_attempt_stop", stop)
    with pytest.raises(ValueError):
        reviews.apply_decision(candidate, decision("stop"))


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"action":"approve","reason":"","guidance":""}',
        '{"action":"delete","reason":"x","guidance":""}',
        "not json",
    ],
)
def test_malformed_model_response_never_approves(raw):
    with pytest.raises(ValueError):
        reviews._decision(raw)


def test_review_runner_uses_astra_and_reserved_capacity(db, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from factory.execution import execution_api

    pid = seed(db, age=1801)
    calls = []

    async def observe(candidate):
        candidate["live_guest"] = {"session_id": "guest", "state": "running"}

    async def run(prompt, **kwargs):
        assert "Implement the feature" in prompt
        calls.append(kwargs)
        return SimpleNamespace(
            terminal_reason="completed", result=json.dumps(decision())
        )

    monkeypatch.setattr(reviews, "observe_guest", observe)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    asyncio.run(reviews.review_once())
    assert calls[0]["model"] == "astra"
    assert calls[0]["admission_tier"] == "interactive"
    assert calls[0]["read_timeout"] == 240
    with Session(db) as session:
        assert session.get(ReservationReview, pid).state == "approved"


def test_cancelled_reviewer_does_not_extend_lease(db, monkeypatch):
    import asyncio
    from factory.execution import execution_api

    pid = seed(db, age=1801)

    async def observe(candidate):
        pass

    async def run(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(reviews, "observe_guest", observe)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(reviews.review_once())
    with Session(db) as session:
        assert session.get(ReservationReview, pid).state == "blocked"
        assert not leases.may_dispatch(
            session, session.get(AgentCapacityReservation, pid)
        )


def test_stopped_review_guidance_reaches_next_planner(db):
    pid = seed(db, key="workflow")
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        permit.state = "settled"
        session.add(permit)
        session.add(SwarmTask(id="task", task_text="work", conductor_model="astra"))
        session.add(
            SwarmNodeRun(
                task_id="task",
                node_key="work",
                attempt=1,
                status="failed",
                dispatch_key="workflow",
            )
        )
        session.add(
            ReservationReview(
                permit_id=pid,
                identity_sha256=leases.identity(permit),
                lease_expires_at=NOW,
                state="stopping",
                verdict="steer",
                guidance="Use the existing broker",
                rationale="Avoid duplicate infrastructure",
            )
        )
        session.commit()
    assert reviews.planner_guidance("task")[0]["guidance"] == "Use the existing broker"


def test_expired_lease_fences_dispatch_without_refunding_capacity(db, monkeypatch):
    pid = seed(db, age=1801, state="reserved", guest=None)
    monkeypatch.setattr(reviews.admission, "get_engine", lambda: db)
    with Session(db) as session:
        sid = session.get(AgentCapacityReservation, pid).session_id
    assert not reviews.admission.recheck(sid, 1, "executor")
    with Session(db) as session:
        assert session.get(AgentCapacityReservation, pid).state == "reserved"
    candidate = reviews.claim_review()
    reviews.apply_decision(candidate, decision("approve"))
    assert reviews.admission.recheck(sid, 1, "executor")


@pytest.mark.parametrize("field", ["generation", "invoke_started_at"])
def test_review_observation_rejects_replacement_invocation(db, monkeypatch, field):
    import asyncio
    from factory.execution.transport import EmberVmShimTransport

    _, candidate = ready(db)
    candidate.pop("live_guest")
    view = {
        "session_id": "guest",
        "state": "running",
        "generation": 1,
        "invoke_started_at": 100,
    }

    async def get_session(self, guest):
        return dict(view)

    monkeypatch.setattr(EmberVmShimTransport, "get_session", get_session)
    asyncio.run(reviews.observe_guest(candidate))
    view[field] += 1
    with pytest.raises(ValueError, match="invocation changed"):
        asyncio.run(reviews.observe_guest(candidate))


def test_settled_reviewer_with_retained_guest_blocks_and_fails_health(db):
    pid = seed(db, key=leases.PREFIX + "cleanup", state="settled", age=300)
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        agent = session.get(AgentSession, permit.session_id)
        agent.created_at = NOW - timedelta(seconds=300)
        session.add(agent)
        session.commit()
        sid = agent.id
    assert reviews.claim_review() is None
    health = reviews.health_snapshot()
    assert not health["ok"]
    assert health["retained_reviewers"] == [sid]
