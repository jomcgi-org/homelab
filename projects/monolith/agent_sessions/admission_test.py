import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import admission, store
from agent_sessions.constants import UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from agent_sessions.transport import Turn


@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admission.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={"schema_translate_map": {"agent_sessions": None}},
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentSession,
                AgentTurn,
                PendingMessage,
            )
        ],
    )
    monkeypatch.setattr(store, "get_engine", lambda: engine)
    monkeypatch.setattr(admission, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def reserve(engine, key, tier="interactive", **kwargs):
    with Session(engine) as db:
        result = admission.reserve_start(db, key, tier=tier, model="luna", **kwargs)
        db.commit()
        return result


def queued(engine, key, tier="interactive"):
    with Session(engine) as db:
        agent = store.create_session(
            db, key, "<guest>", "main", "luna", admission_tier=tier
        )
        store.create_pending_message(db, agent.id, "work", "luna")
        return agent.id


def result():
    return Turn(
        result="done",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="cli",
        usage={},
        total_cost_usd=None,
        duration_ms=1,
        activities=[],
        model="luna",
    )


def test_concurrent_claims_share_last_permit_and_original_pending(database):
    for n in range(3):
        assert reserve(database, f"occupied-{n}")
    candidates = [queued(database, f"candidate-{n}") for n in range(2)]
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(
            pool.map(
                lambda sid: store.claim_pending_message_for_session_sync(sid, str(sid)),
                candidates,
            )
        )
    assert sorted(outcomes, key=lambda value: value or 0) == [None, 1]
    with Session(database) as db:
        assert len(db.exec(select(AgentCapacityReservation)).all()) == 4
        pending = db.exec(select(PendingMessage)).all()
        assert sorted(p.dispatch_count for p in pending) == [0, 1]


def test_two_kg_project_and_interactive_fit_but_no_third_kg(database):
    assert reserve(database, "kg-1", "kg")
    assert reserve(database, "kg-2", "kg")
    assert not reserve(database, "kg-3", "kg")
    assert reserve(database, "project", "project")
    assert not reserve(database, "probe", "probe")
    assert reserve(database, "human")
    assert not reserve(database, "next-human")


def test_stale_uncertain_capacity_warning_is_rate_limited(
    database, monkeypatch, caplog
):
    assert reserve(database, "kg-held", "kg")
    assert reserve(database, "project", "project")
    assert reserve(database, "probe", "probe")
    with Session(database) as db:
        held = admission.reservation(db, "kg-held")
        held.state = "uncertain"
        held.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)
        held_id = held.id
        db.add(held)
        db.commit()
    clock = [100.0]
    monkeypatch.setattr(admission.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(admission, "_last_uncertain_warning_at", float("-inf"))
    with caplog.at_level("WARNING", logger=admission.__name__):
        assert not reserve(database, "blocked-one", "kg")
        assert not reserve(database, "blocked-two", "kg")
        clock[0] += admission.UNCERTAIN_WARNING_INTERVAL_SECONDS + 1
        assert not reserve(database, "blocked-three", "kg")
    warnings = [
        record.message
        for record in caplog.records
        if "Stale uncertain permits" in record.message
    ]
    assert len(warnings) == 2
    assert all(
        f"id={held_id}" in warning
        and "tier=kg" in warning
        and "age_seconds=" in warning
        for warning in warnings
    )


def test_total_limit_warns_interactive_about_stale_background_hold(
    database, monkeypatch, caplog
):
    assert reserve(database, "kg-held", "kg")
    for index in range(admission.total_limit() - 1):
        assert reserve(database, f"interactive-{index}")
    with Session(database) as db:
        held = admission.reservation(db, "kg-held")
        held.state = "uncertain"
        held.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)
        held_id = held.id
        db.add(held)
        db.commit()
    monkeypatch.setattr(admission, "_last_uncertain_warning_at", float("-inf"))
    with caplog.at_level("WARNING", logger=admission.__name__):
        assert not reserve(database, "blocked-interactive")
    assert any(
        f"id={held_id} tier=kg" in record.message
        for record in caplog.records
        if "Stale uncertain permits" in record.message
    )


def test_waiting_interactive_wins_next_admission(database):
    human = queued(database, "human")
    kg = queued(database, "kg", "kg")
    assert store.claim_pending_message_for_session_sync(kg, "worker") is None
    assert store.claim_pending_message_for_session_sync(human, "human-worker") == 1
    assert store.claim_pending_message_for_session_sync(kg, "worker") == 1


def test_later_interactive_message_does_not_block_background(database):
    human = queued(database, "human")
    assert store.claim_pending_message_for_session_sync(human, "worker") == 1
    with Session(database) as db:
        store.create_pending_message(db, human, "later", "luna")
    assert reserve(database, "kg", "kg")


def test_pre_session_reservation_binds_once_without_double_charge(database):
    assert reserve(database, "workflow-job", "kg")
    sid = queued(database, "workflow-job", "project")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    assert admission.recheck(sid, 1, "worker")
    assert not admission.recheck(sid, 1, "other-worker")
    with Session(database) as db:
        row = admission.reservation(db, "workflow-job")
        assert row.session_id == sid and row.state == "running" and row.tier == "kg"
        assert len(db.exec(select(AgentCapacityReservation)).all()) == 1
        assert db.get(AgentSession, sid).admission_tier == "kg"


def test_terminal_execution_frees_permit_even_with_unknown_cost(database):
    sid = queued(database, "kg", "kg")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    assert admission.recheck(sid, 1, "worker")
    assert reserve(database, "kg-2", "kg")
    assert not reserve(database, "kg-3", "kg")
    store.persist_turn_from_pending_sync(
        sid, 1, "work", result(), "done", "completed", claim_owner="worker"
    )
    assert reserve(database, "kg-3", "kg")
    with Session(database) as db:
        assert admission.reservation(db, "kg").state == "settled"
        assert db.exec(select(AgentTurn)).one().cost_usd is None


def test_unknown_outlives_deleted_pending_and_observer_lease(database):
    sid = queued(database, "kg", "kg")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    assert admission.recheck(sid, 1, "worker")
    assert store.release_pending_message_claim_sync(sid, 1, "worker")
    with Session(database) as db:
        row = admission.reservation(db, "kg")
        row.created_at = datetime.now(timezone.utc) - timedelta(days=10)
        db.add(row)
        db.commit()
        assert db.exec(select(PendingMessage)).first() is None
        assert row.state == "uncertain"
    assert not admission.recheck(sid, 1, "worker")
    assert reserve(database, "kg-2", "kg")
    assert not reserve(database, "kg-3", "kg")


def test_unknown_can_settle_only_with_confirmed_cessation(database):
    sid = queued(database, "kg", "kg")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    assert admission.recheck(sid, 1, "worker")
    store.release_pending_message_claim_sync(sid, 1, "worker")
    with Session(database) as db:
        agent = db.get(AgentSession, sid)
        admission.settle(db, agent, 1, outcome="timeout", cessation_confirmed=False)
        db.commit()
        assert admission.reservation(db, "kg").state == "uncertain"
        admission.confirm_guest_cessation(db, agent)
        db.commit()
        assert admission.reservation(db, "kg").state == "settled"
    assert reserve(database, "other", "kg")
    with Session(database) as db:
        # A subsequent adoption scan cannot reopen the historical unknown turn.
        assert admission.reservation(db, "kg").state == "settled"


def test_legacy_active_and_unknown_are_adopted_even_above_cap(database):
    with Session(database) as db:
        for n in range(5):
            agent = store.create_session(
                db, f"old-{n}", "guest", "main", "luna", admission_tier="kg"
            )
            if n == 0:
                db.add(
                    PendingMessage(
                        session_id=agent.id,
                        seq=1,
                        message_text="work",
                        dispatch_count=1,
                        claimed_by_replica="old",
                    )
                )
            else:
                db.add(
                    AgentTurn(
                        session_id=agent.id,
                        seq=1,
                        prompt="work",
                        result_text="error",
                        terminal_reason="error",
                        stop_reason=UNKNOWN_INVOCATION,
                    )
                )
            db.commit()
    assert not reserve(database, "new")
    with Session(database) as db:
        assert len(db.exec(select(AgentCapacityReservation)).all()) == 5


def test_reconciled_error_without_guest_or_unknown_stop_is_not_reopened(database):
    with Session(database) as db:
        agent = store.create_session(
            db, "old-luna", "guest", "main", "luna", admission_tier="project"
        )
        agent.status = "warn"
        db.add(agent)
        db.add(
            AgentTurn(
                session_id=agent.id,
                seq=1,
                prompt="work",
                result_text="error",
                terminal_reason="error",
                stop_reason=None,
                cost_usd=None,
            )
        )
        db.commit()
    assert reserve(database, "new", "project")
    with Session(database) as db:
        assert admission.reservation(db, "old-luna") is None


def test_atomic_daily_allowance_reserves_unbound_then_counts_bound_once(database):
    args = dict(daily_key="kg-rolling-24h", daily_limit=400, daily_used=399)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda key: reserve(database, key, "kg", **args), ["a", "b"])
        )
    assert sorted(results) == [False, True]
    key = "a" if results[0] else "b"
    assert reserve(database, key, "kg", **args)  # exact replay
    queued(database, key, "kg")
    assert not reserve(database, "next", "kg", **{**args, "daily_used": 400})


def test_only_uncreated_reserved_session_can_cancel(database):
    assert reserve(database, "uncreated", "kg")
    with Session(database) as db:
        assert admission.cancel_unbound(db, "uncreated")
        db.commit()
    assert not reserve(database, "uncreated", "kg")
    assert reserve(database, "created", "kg")
    queued(database, "created", "kg")
    with Session(database) as db:
        assert not admission.cancel_unbound(db, "created")


def test_synthetic_direct_path_uses_real_admission_without_transport(
    database, monkeypatch
):
    from agent_sessions import execution_api as api

    for n in range(4):
        assert reserve(database, f"occupied-{n}")

    def persist(local_id, workspace, branch, model, repo, **kwargs):
        with Session(database) as db:
            return store.create_session(
                db, local_id, workspace, branch, model, repo, **kwargs
            )

    def pending(sid, prompt, model):
        with Session(database) as db:
            return store.create_pending_message(db, sid, prompt, model).seq

    async def forbidden(*args, **kwargs):
        pytest.fail("Capacity denial must precede direct synthetic transport")

    monkeypatch.setattr(api, "_persist_session", persist)
    monkeypatch.setattr(api, "_persist_pending_message", pending)
    monkeypatch.setattr(
        api, "_claim_pending_message_sync", store.claim_pending_message_for_session_sync
    )
    monkeypatch.setattr(api._transport, "deliver", forbidden)
    assert asyncio.run(api.run_synthetic_session("probe")) is None
    with Session(database) as db:
        assert db.exec(select(AgentSession)).one().admission_tier == "probe"
        assert db.exec(select(PendingMessage)).one().dispatch_count == 0


def test_correction_inherits_routine_fence_and_unknown_never_expires(database):
    assert reserve(database, "job", "kg", routine_job_name="kg:raw")
    sid = queued(database, "job", "kg")
    assert store.claim_pending_message_for_session_sync(sid, "first") == 1
    store.persist_turn_from_pending_sync(
        sid, 1, "work", result(), "done", "completed", claim_owner="first"
    )
    with Session(database) as db:
        store.create_pending_message(db, sid, "correction", "luna")
    assert store.claim_pending_message_for_session_sync(sid, "second") == 2
    assert admission.recheck(sid, 2, "second")
    store.release_pending_message_claim_sync(sid, 2, "second")
    with Session(database) as db:
        assert admission.reservation(db, "job", 2).routine_job_name == "kg:raw"
        assert admission.reserved_routine_jobs(db) == {"kg:raw"}
    assert not reserve(database, "duplicate-job", "kg", routine_job_name="kg:raw")


def test_transport_uncertainty_blocks_followup_and_confirmed_turn_does_not(database):
    sid = queued(database, "session")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    assert admission.recheck(sid, 1, "worker")
    store.mark_turn_error_sync(sid, 1, "response lost", "worker")
    with Session(database) as db:
        assert admission.reservation(db, "session").state == "uncertain"
        assert store.has_unknown_outcome(db, sid)
        with pytest.raises(store.SessionOutcomeUnknown):
            store.create_pending_message(db, sid, "retry", "luna")


def test_replay_cannot_reclassify_permit_or_change_model(database):
    assert reserve(database, "job", "kg")
    assert not reserve(database, "job", "interactive")
    with Session(database) as db:
        assert not admission.reserve_start(db, "job", tier="kg", model="opus")
        assert admission.reservation(db, "job").tier == "kg"


def test_waiting_project_precedes_new_kg_start(database):
    project = queued(database, "project", "project")
    kg = queued(database, "kg", "kg")
    assert store.claim_pending_message_for_session_sync(kg, "kg-worker") is None
    assert store.claim_pending_message_for_session_sync(project, "project-worker") == 1
    assert store.claim_pending_message_for_session_sync(kg, "kg-worker") == 1


def test_recorded_workload_and_followup_model_are_pinned(database):
    sid = queued(database, "session")
    assert store.claim_pending_message_for_session_sync(sid, "first") == 1
    assert admission.recheck(sid, 1, "first", "claude-runtime")
    assert not admission.recheck(sid, 1, "first", "other-runtime")
    store.persist_turn_from_pending_sync(
        sid, 1, "work", result(), "done", "completed", claim_owner="first"
    )
    with Session(database) as db:
        store.create_pending_message(db, sid, "followup", "terra")
    assert store.claim_pending_message_for_session_sync(sid, "second") == 2
    with Session(database) as db:
        assert admission.reservation(db, "session", 1).workload == "claude-runtime"
        assert admission.reservation(db, "session", 2).model == "terra"


def test_cancel_unattempted_cannot_release_claimed_or_unknown(database):
    assert reserve(database, "unstarted", "kg")
    sid = queued(database, "unstarted", "kg")
    with Session(database) as db:
        agent = db.get(AgentSession, sid)
        pending = store.get_pending_message(db, sid, 1)
        assert admission.cancel_unattempted(db, agent, pending)
        db.delete(pending)
        db.commit()
        assert admission.reservation(db, "unstarted").state == "settled"
    sid = queued(database, "started", "kg")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    with Session(database) as db:
        assert not admission.cancel_unattempted(
            db, db.get(AgentSession, sid), store.get_pending_message(db, sid, 1)
        )
        assert admission.reservation(db, "started").state == "reserved"


def test_binding_clear_is_not_cessation_evidence(database):
    sid = queued(database, "session")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    assert admission.recheck(sid, 1, "worker")
    with Session(database) as db:
        agent = db.get(AgentSession, sid)
        agent.ember_session_id = "guest"
        db.add(agent)
        db.commit()
    store.mark_turn_error_sync(sid, 1, "lost response", "worker")
    with Session(database) as db:
        store.clear_ember_bindings_by_ember_id(db, "guest")
        assert admission.reservation(db, "session").state == "uncertain"
        assert store.has_unknown_outcome(db, sid)


def test_operator_destroy_retains_a_binding_under_an_unknown_outcome(database):
    """The exact 2026-09-09 race. monolith_agent_session_destroy reaches
    store.clear_ember_bindings_by_ember_id through agent_sessions/mcp.py. Run
    while the control plane is still tearing the guest down, a clear would
    leave a row that permit supervision reads as "no guest was ever bound", so
    the clear is refused here the way retire_guest_cleanup already refuses it.
    """
    sid = queued(database, "destroy-unknown")
    assert store.claim_pending_message_for_session_sync(sid, "worker") == 1
    assert admission.recheck(sid, 1, "worker")
    with Session(database) as db:
        agent = db.get(AgentSession, sid)
        agent.ember_session_id = "guest-destroy"
        db.add(agent)
        db.commit()
    store.mark_turn_error_sync(sid, 1, "lost response", "worker")
    with Session(database) as db:
        assert store.has_unknown_outcome(db, sid)
        assert store.clear_ember_bindings_by_ember_id(db, "guest-destroy") == []
        agent = db.get(AgentSession, sid)
        assert agent.ember_session_id == "guest-destroy"
        assert agent.prior_ember_lineage_id is None
        assert admission.reservation(db, "destroy-unknown").state == "uncertain"


def test_operator_destroy_still_clears_a_resolved_binding(database):
    """The refusal is scoped to an unresolved outcome, so an ordinary destroy
    of a parked or finished session still clears and still preserves the
    lineage handle store.set_ember_session recorded.
    """
    with Session(database) as db:
        agent = store.create_session(db, "destroy-resolved", "<guest>", "main", "luna")
        agent.ember_session_id = "guest-resolved"
        agent.ember_lineage_id = "lineage-resolved"
        agent.cli_session_id = "cli-resolved"
        db.add(agent)
        db.commit()
        sid = agent.id
    with Session(database) as db:
        assert store.clear_ember_bindings_by_ember_id(db, "guest-resolved") == [sid]
        agent = db.get(AgentSession, sid)
        assert agent.ember_session_id is None
        assert agent.ember_lineage_id is None
        assert agent.prior_ember_lineage_id == "lineage-resolved"
        assert agent.prior_cli_session_id == "cli-resolved"


def test_legacy_routine_identity_is_adopted_from_trusted_workflow_fields(database):
    with Session(database) as db:
        agent = store.create_session(
            db,
            "workflow:kg-drain:kg:raw",
            "guest",
            "main",
            "luna",
            workflow_id="workflow",
            node_key="kg-drain",
            admission_tier="kg",
        )
        db.add(
            PendingMessage(
                session_id=agent.id,
                seq=1,
                message_text="work",
                dispatch_count=1,
                claimed_by_replica="old",
            )
        )
        db.commit()
        admission.adopt_existing(db)
        db.commit()
        assert admission.reserved_routine_jobs(db) == {"kg:raw"}
    assert not reserve(database, "new-owner", "kg", routine_job_name="kg:raw")


@pytest.mark.parametrize("allowed", [False, True])
def test_factory_priority_requires_current_start_permission(
    database, monkeypatch, allowed
):
    from swarm import factory_controls

    queued(database, "factory:task:node:1", "project")
    monkeypatch.setattr(
        factory_controls, "can_start", lambda *_args, **_kwargs: {"ok": allowed}
    )
    assert reserve(database, "kg", "kg") is (not allowed)


def test_free_background_slots_reads_the_pool_without_reserving(database):
    with Session(database) as db:
        assert admission.free_background_slots(db) == admission.background_limit()
    assert reserve(database, "one", tier="project")
    assert reserve(database, "two", tier="kg")
    with Session(database) as db:
        assert admission.free_background_slots(db) == 1
        # Reading twice returns the same answer: this grants nothing.
        assert admission.free_background_slots(db) == 1
    assert reserve(database, "three", tier="probe")
    with Session(database) as db:
        assert admission.free_background_slots(db) == 0
    assert not reserve(database, "four", tier="project")


def test_free_background_slots_respects_the_total_limit(database):
    for index in range(admission.total_limit() - 1):
        assert reserve(database, f"interactive-{index}", tier="interactive")
    with Session(database) as db:
        assert admission.free_background_slots(db) == 1


def test_admission_limits_default_to_the_shipped_numbers(monkeypatch):
    for name in (
        "AGENT_ADMISSION_TOTAL",
        "AGENT_ADMISSION_BACKGROUND",
        "AGENT_ADMISSION_KG",
    ):
        monkeypatch.delenv(name, raising=False)
    assert admission.total_limit() == 4
    assert admission.background_limit() == 3
    assert admission.kg_limit() == 2


def test_admission_limits_read_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_ADMISSION_TOTAL", "16")
    monkeypatch.setenv("AGENT_ADMISSION_BACKGROUND", "12")
    monkeypatch.setenv("AGENT_ADMISSION_KG", "2")
    assert admission.total_limit() == 16
    assert admission.background_limit() == 12
    assert admission.kg_limit() == 2


@pytest.mark.parametrize("value", ["", "   ", "many", "0", "-3"])
def test_admission_limits_ignore_an_unusable_value(monkeypatch, value):
    monkeypatch.setenv("AGENT_ADMISSION_TOTAL", value)
    assert admission.total_limit() == 4


def test_a_narrower_total_bounds_the_inner_limits(monkeypatch):
    monkeypatch.setenv("AGENT_ADMISSION_TOTAL", "2")
    monkeypatch.setenv("AGENT_ADMISSION_BACKGROUND", "12")
    monkeypatch.setenv("AGENT_ADMISSION_KG", "9")
    assert admission.background_limit() == 2
    assert admission.kg_limit() == 2


def test_a_raised_total_admits_more_background_sessions(database, monkeypatch):
    monkeypatch.setenv("AGENT_ADMISSION_TOTAL", "16")
    monkeypatch.setenv("AGENT_ADMISSION_BACKGROUND", "12")
    monkeypatch.setenv("AGENT_ADMISSION_KG", "2")
    for index in range(12):
        assert reserve(database, f"project-{index}", tier="project")
    assert not reserve(database, "project-over", tier="project")
    assert reserve(database, "interactive-one")


def test_a_raised_background_limit_still_bounds_the_kg_tier(database, monkeypatch):
    monkeypatch.setenv("AGENT_ADMISSION_TOTAL", "16")
    monkeypatch.setenv("AGENT_ADMISSION_BACKGROUND", "12")
    monkeypatch.setenv("AGENT_ADMISSION_KG", "2")
    assert reserve(database, "kg-one", tier="kg")
    assert reserve(database, "kg-two", tier="kg")
    assert not reserve(database, "kg-three", tier="kg")
    assert reserve(database, "project-one", tier="project")
