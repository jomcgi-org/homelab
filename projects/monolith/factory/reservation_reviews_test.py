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
from factory.orchestration.factory_models import FactoryAudit, FactoryStart
from factory.execution.models import ProbeObservation

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
                FactoryStart,
                FactoryAudit,
                ProbeObservation,
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
            admission_tier=tier,
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
                last_dispatch_at=NOW - timedelta(seconds=age),
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


def seed_cost_attempt(db, *, provider_cost=None, list_cost=None, ceiling=2.0):
    workflow = "factory-node:task:work:1"
    pid = seed(db, key=workflow)
    pin = {
        "task_id": "task",
        "node_key": "work",
        "attempt": 1,
        "workflow_id": workflow,
        "model": "sol",
        "max_cost_usd": ceiling,
    }
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        agent = session.get(AgentSession, permit.session_id)
        agent.node_key = "work"
        agent.node_attempt = 1
        session.add(agent)
        session.add(SwarmTask(id="task", task_text="work", conductor_model="astra"))
        session.add(
            SwarmNodeRun(
                task_id="task",
                node_key="work",
                attempt=1,
                dispatch_key=workflow,
                pin_json=json.dumps(pin),
                session_id=agent.id,
                status="dispatched",
            )
        )
        session.add(
            FactoryStart(
                task_id="task",
                start_key=workflow,
                actor="test",
                model="sol",
                max_cost_usd=ceiling,
                status="reserved",
                session_id=agent.id,
            )
        )
        session.add(
            AgentTurn(
                session_id=agent.id,
                seq=1,
                prompt="work",
                result_text="progress",
                cost_usd=provider_cost,
                list_cost_usd=list_cost,
            )
        )
        session.commit()
        return {
            "permit_id": pid,
            "session_id": agent.id,
            "workflow_id": workflow,
        }


def install_cost_stop_fakes(db, monkeypatch, *, persist=False):
    from factory.orchestration import factory_attempt_stop as stops

    calls = []

    def read(task_id, node_key, attempt, session_id):
        assert (task_id, node_key, attempt) == ("task", "work", 1)
        return {"identity_sha256": "a" * 64, "session_id": session_id}

    def stop(**kwargs):
        calls.append(kwargs)
        with Session(db) as session:
            kwargs["authorization_check"](session)
            if persist:
                session.add(
                    FactoryAudit(
                        actor=kwargs["actor"],
                        action=stops.REQUEST_ACTION,
                        task_id=kwargs["task_id"],
                        detail_json=json.dumps(
                            {"identity": {"workflow_id": "factory-node:task:work:1"}}
                        ),
                    )
                )
                session.commit()
        return {"ok": True}

    monkeypatch.setattr(stops, "read_attempt_stop", read)
    monkeypatch.setattr(stops, "request_attempt_stop", stop)
    return calls


def test_provider_cost_above_ceiling_requests_distinct_stop(db, monkeypatch):
    attempt = seed_cost_attempt(db, provider_cost=1.01)
    with Session(db) as session:
        session.add(
            AgentTurn(
                session_id=attempt["session_id"],
                seq=2,
                prompt="continue",
                result_text="more progress",
                cost_usd=1.0,
            )
        )
        session.commit()
    monkeypatch.setenv("FACTORY_ENFORCE_COST_CEILING_ENABLED", "true")
    calls = install_cost_stop_fakes(db, monkeypatch)

    reviews.enforce_cost_ceilings()

    assert len(calls) == 1
    assert calls[0]["session_id"] == attempt["session_id"]
    assert calls[0]["reason"] == reviews.COST_CEILING_REASON
    assert calls[0]["actor"] == reviews.COST_CEILING_ACTOR
    assert calls[0]["request_key"] == "cost-ceiling:" + "a" * 64


def test_repeated_cost_checks_do_not_request_a_second_stop(db, monkeypatch):
    seed_cost_attempt(db, provider_cost=3.0)
    monkeypatch.setenv("FACTORY_ENFORCE_COST_CEILING_ENABLED", "true")
    calls = install_cost_stop_fakes(db, monkeypatch, persist=True)

    reviews.enforce_cost_ceilings()
    reviews.enforce_cost_ceilings()

    assert len(calls) == 1


def test_provider_cost_under_ceiling_is_not_stopped(db, monkeypatch):
    seed_cost_attempt(db, provider_cost=1.99)
    monkeypatch.setenv("FACTORY_ENFORCE_COST_CEILING_ENABLED", "true")
    calls = install_cost_stop_fakes(db, monkeypatch)

    reviews.enforce_cost_ceilings()

    assert calls == []


def test_list_price_above_ceiling_is_not_stopped(db, monkeypatch):
    seed_cost_attempt(db, provider_cost=None, list_cost=3.0)
    monkeypatch.setenv("FACTORY_ENFORCE_COST_CEILING_ENABLED", "true")
    calls = install_cost_stop_fakes(db, monkeypatch)

    reviews.enforce_cost_ceilings()

    assert calls == []


def test_disabled_cost_ceiling_does_not_look_up_spend(db, monkeypatch):
    seed_cost_attempt(db, provider_cost=3.0)
    monkeypatch.setenv("FACTORY_ENFORCE_COST_CEILING_ENABLED", "false")
    monkeypatch.setattr(
        reviews,
        "get_engine",
        lambda: pytest.fail("disabled enforcement must not query spend"),
    )

    reviews.enforce_cost_ceilings()


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


def test_active_session_without_exact_permit_is_unhealthy(db):
    seed(db, state="settled")
    with Session(db) as session:
        pending = session.exec(select(PendingMessage)).one()
        pending.created_at = NOW - timedelta(seconds=1801)
        session.add(pending)
        session.commit()
        sid = pending.session_id
    health = reviews.health_snapshot()
    assert not health["ok"]
    assert health["uncovered_sessions"] == [sid]


def test_unbound_reservation_can_be_cancelled_with_positive_local_proof(db):
    with Session(db) as session:
        permit = AgentCapacityReservation(
            local_session_id="unbound",
            pending_seq=1,
            tier="project",
            model="sol",
            created_at=NOW - timedelta(seconds=1801),
        )
        session.add(permit)
        session.commit()
        pid = permit.id
    candidate = reviews.claim_review()
    reviews.apply_decision(candidate, decision("stop"))
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        assert permit.state == "settled"
        assert permit.outcome == "cancelled_before_session"


def test_nonfactory_stop_fences_exact_turn_and_retries_conditional_destroy(
    db, monkeypatch
):
    import asyncio
    from core import db as core_db
    from factory.execution.transport import EmberVmShimTransport

    pid, candidate = ready(db)
    candidate["stop_precondition"] = {
        "session_id": "guest",
        "generation": 1,
        "invoke_started_at": 100,
    }
    monkeypatch.setattr(core_db, "get_engine", lambda: db)
    reviews.apply_decision(candidate, decision("steer"))
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        sid = permit.session_id
        assert permit.state == "uncertain"
        assert session.exec(select(PendingMessage)).first() is None
        assert session.exec(select(AgentTurn)).one().cost_usd is None
        assert session.get(ReservationReview, pid).state == "stopping"
    assert leases.stop_requested(sid, 1, "executor", 1)
    assert not leases.stop_requested(sid, 1, "replacement", 1)
    assert not leases.stop_requested(sid, 1, "executor", 2)
    calls = []

    async def destroy(self, guest, *, stop_precondition):
        calls.append((guest, stop_precondition))
        return {"state": "destroying"}

    monkeypatch.setattr(EmberVmShimTransport, "destroy_session", destroy)
    asyncio.run(reviews.process_execution_stops())
    asyncio.run(reviews.process_execution_stops())
    assert calls == [("guest", candidate["stop_precondition"])] * 2
    reviews._failed(candidate, "Cancelled after committing stop")
    with Session(db) as session:
        assert session.get(ReservationReview, pid).state == "stopping"
        assert session.get(AgentCapacityReservation, pid).state == "uncertain"
        agent = session.get(AgentSession, sid)
        agent.ember_session_id = "replacement"
        session.add(agent)
        session.commit()
    asyncio.run(reviews.process_execution_stops())
    assert len(calls) == 2


def test_routine_steering_is_available_only_after_cessation(db):
    pid, candidate = ready(db)
    candidate["stop_precondition"] = {"session_id": "guest"}
    reviews.apply_decision(candidate, decision("steer"))
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        permit.routine_job_name = "kg:source"
        session.add(permit)
        session.commit()
    assert reviews.routine_guidance("kg:source") == ""
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        permit.state = "settled"
        session.add(permit)
        session.commit()
    assert "Check the schema" in reviews.routine_guidance("kg:source")


def test_bound_unattempted_stop_preserves_queued_input(db):
    pid = seed(db, state="reserved", guest=None)
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        permit.owner = None
        pending = session.exec(select(PendingMessage)).one()
        pending.dispatch_count = 0
        pending.claimed_by_replica = None
        pending.last_dispatch_at = None
        pending.partial_text = None
        session.add(permit)
        session.add(pending)
        session.commit()
    reviews.apply_decision(reviews.claim_review(), decision("stop"))
    with Session(db) as session:
        assert session.get(AgentCapacityReservation, pid).state == "settled"
        assert (
            session.exec(select(PendingMessage)).one().message_text
            == "Implement the feature"
        )
        assert session.exec(select(AgentSession)).one().status == "failed"


@pytest.mark.parametrize("later_claimed", [False, True])
def test_reviewed_interactive_stop_settles_only_with_safe_preserved_queue(
    db, monkeypatch, later_claimed
):
    import asyncio
    from factory.execution import permit_supervision as observer

    pid, candidate = ready(db, tier="interactive")
    started = int((NOW - timedelta(seconds=1600)).timestamp() * 1000)
    precondition = dict(
        session_id="guest",
        generation=1,
        invoke_started_at=started,
        vm_id="vm",
        node_id="node",
        instance_id="node/pod",
        pod_uid="pod",
        boot_id="boot",
    )
    candidate["stop_precondition"] = precondition
    reviews.apply_decision(candidate, decision("stop"))
    with Session(db) as session:
        permit = session.get(AgentCapacityReservation, pid)
        sid = permit.session_id
        turn = session.exec(select(AgentTurn)).one()
        turn.created_at = NOW - timedelta(seconds=10)
        session.add(turn)
        session.add(
            PendingMessage(
                session_id=sid,
                seq=2,
                message_text="Keep this user input",
                claimed_by_replica="other" if later_claimed else None,
                dispatch_count=1 if later_claimed else 0,
            )
        )
        session.commit()
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    monkeypatch.setattr(observer, "get_engine", lambda: db)
    monkeypatch.setattr(observer, "_now", lambda: NOW)
    intent = dict(
        precondition,
        operation_id="stop-1",
        requested_at_unix_ms=int((NOW - timedelta(seconds=5)).timestamp() * 1000),
    )

    class Transport:
        async def get_session(self, guest):
            return dict(
                session_id=guest,
                state="destroyed",
                generation=1,
                invoke_started_at=started,
                last_invoke_at=None,
                updated_at=int((NOW - timedelta(seconds=1)).timestamp() * 1000),
                stop_intent=intent,
                stop_completion=dict(
                    intent,
                    completed_at_unix_ms=int(
                        (NOW - timedelta(seconds=1)).timestamp() * 1000
                    ),
                ),
            )

    asyncio.run(observer.sweep_once(Transport()))
    with Session(db) as session:
        assert session.get(AgentCapacityReservation, pid).state == (
            "uncertain" if later_claimed else "settled"
        )
        assert (
            session.exec(select(PendingMessage)).one().message_text
            == "Keep this user input"
        )
