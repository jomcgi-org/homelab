import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import admission, permit_supervision as supervision
from agent_sessions.constants import UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from agent_sessions.permit_supervision import ProbeObservation
from swarm.factory_models import FactoryStart
from swarm.models import SwarmNodeRun, SwarmTask

_DEFAULT_ROUTINE = object()


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'probes.db'}",
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
                AgentSession,
                AgentTurn,
                PendingMessage,
                AgentCapacityPool,
                AgentCapacityReservation,
                ProbeObservation,
                SwarmTask,
                FactoryStart,
                SwarmNodeRun,
            )
        ],
    )
    # The routine job table is raw SQL in agent/routine_jobs.py rather than a
    # SQLModel, so mirror the columns hold_job_for_unknown_outcome writes.
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE routine_jobs (name TEXT PRIMARY KEY, "
                "routine_kind TEXT, last_status TEXT, last_summary TEXT, "
                "next_run_at TIMESTAMP, locked_by TEXT, locked_at TIMESTAMP)"
            )
        )
    monkeypatch.setenv("AGENT_PROBE_SUPERVISION_ENABLED", "true")
    monkeypatch.setattr(supervision, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def seed(
    engine,
    key="one",
    tier="probe",
    legacy=False,
    *,
    guest_bound=True,
    turn_count=1,
    dispatch_count=None,
    workflow_id=None,
    node_key=None,
    routine_job_name=_DEFAULT_ROUTINE,
    local_session_id=None,
    claimed=True,
    recovery_updates=None,
    history=0,
    job_held=False,
):
    at = datetime.now(timezone.utc) - timedelta(seconds=10)
    if routine_job_name is _DEFAULT_ROUTINE:
        routine_job_name = f"job:{key}" if tier in {"kg", "project"} else None
    if tier in {"kg", "project"}:
        workflow_id = workflow_id or f"wf-{key}"
        node_key = node_key or ("kg-drain" if tier == "kg" else "qwen-drain")
    local_session_id = local_session_id or (
        f"synthetic:{key}"
        if tier == "probe"
        else f"{workflow_id}:{node_key}:{routine_job_name}"
        if tier in {"kg", "project"}
        else f"session:{key}"
    )
    with Session(engine) as db, db.begin():
        agent = AgentSession(
            local_session_id=local_session_id,
            workspace="<guest>",
            branch="main",
            admission_tier=tier,
            status="warn" if legacy else "failed",
            workflow_id=workflow_id,
            node_key=node_key,
            ember_session_id="guest-" + key if guest_bound else None,
            ember_session_token="never-export-this-token" if guest_bound else None,
            last_turn_at=at,
        )
        db.add(agent)
        db.flush()
        failed_seq = history + 1
        permit = AgentCapacityReservation(
            local_session_id=agent.local_session_id,
            session_id=agent.id,
            pending_seq=failed_seq,
            tier=tier,
            state="uncertain",
            outcome="delivery_error" if legacy else "executor_cancelled",
            owner="worker" if claimed else None,
            routine_job_name=routine_job_name,
        )
        db.add(permit)
        if routine_job_name is not None:
            db.execute(
                text(
                    "INSERT INTO routine_jobs (name, routine_kind, last_status, "
                    "last_summary, next_run_at) VALUES "
                    "(:name, :kind, :status, :summary, :next_run_at)"
                ),
                {
                    "name": routine_job_name,
                    "kind": "kg-drain" if tier == "kg" else "qwen-drain",
                    "status": UNKNOWN_INVOCATION if job_held else "ok",
                    "summary": f"session_id={agent.id}: lost response",
                    "next_run_at": None if job_held else at,
                },
            )
        for seq in range(1, failed_seq):
            db.add(
                AgentTurn(
                    session_id=agent.id,
                    seq=seq,
                    prompt="earlier prompt",
                    result_text="answered",
                    terminal_reason=None,
                    stop_reason=None,
                    created_at=at - timedelta(seconds=failed_seq - seq),
                )
            )
            # admission.claim_pending calls reserve_start for every turn and
            # admission.settle only flips state, so each completed turn leaves
            # its own settled reservation row behind.
            db.add(
                AgentCapacityReservation(
                    local_session_id=agent.local_session_id,
                    session_id=agent.id,
                    pending_seq=seq,
                    tier=tier,
                    state="settled",
                    outcome="completed",
                    owner="worker",
                    routine_job_name=routine_job_name,
                    settled_at=at - timedelta(seconds=failed_seq - seq),
                )
            )
        if turn_count:
            usage = {
                "recovery": {
                    "dispatch_count": dispatch_count
                    if dispatch_count is not None
                    else 1,
                    "claim_owner": "worker" if claimed else None,
                    "last_dispatch_at": at.isoformat() if claimed else None,
                    "partial_text": None,
                    "partial_activities": None,
                    **(recovery_updates or {}),
                }
            }
            db.add(
                AgentTurn(
                    session_id=agent.id,
                    seq=failed_seq,
                    prompt="original prompt",
                    result_text="lost",
                    terminal_reason="error",
                    stop_reason=None if legacy else UNKNOWN_INVOCATION,
                    cost_usd=None,
                    created_at=at,
                    artifact_blob=b"retained artifact",
                    usage_json=json.dumps(usage),
                )
            )
        db.flush()
        return permit.id


def before(engine, permit_id):
    with Session(engine) as db:
        permit = db.get(AgentCapacityReservation, permit_id)
        agent = db.get(AgentSession, permit.session_id)
        turns = db.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == agent.id)
            .order_by(AgentTurn.seq)
        ).all()
        return permit.model_dump(), agent.model_dump(), [t.model_dump() for t in turns]


def proof(guest="guest-one", **updates):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return dict(
        {
            "session_id": guest,
            "state": "evicted",
            "generation": 0,
            "invoke_started_at": now - 20000,
            "last_invoke_at": now - 15000,
            "updated_at": now - 1000,
        },
        **updates,
    )


def sweep(response):
    async def get_session(guest):
        if isinstance(response, Exception):
            raise response
        return response(guest) if callable(response) else response

    async def forbidden(*args, **kwargs):
        raise AssertionError("The observer must never invoke or stop a guest")

    asyncio.run(
        supervision.sweep_once(
            SimpleNamespace(
                get_session=get_session,
                destroy_session=forbidden,
                invoke=forbidden,
            )
        )
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_probe_settles_preserving_history_and_other_tiers(database, legacy):
    pid = seed(database, legacy=legacy)
    others = [seed(database, t, tier=t) for t in ("interactive", "project", "kg")]
    original = before(database, pid)
    untouched = [before(database, p) for p in others]
    evidence = proof()
    sweep(evidence)
    after = before(database, pid)
    assert after[0]["state"] == "settled"
    assert after[0]["outcome"] == "guest_cessation_confirmed"
    assert after[1] == original[1]
    assert after[2] == original[2]
    assert [before(database, p) for p in others] == untouched
    with Session(database) as db:
        audit = db.get(ProbeObservation, pid)
        assert audit.settled_at is not None
        assert "never-export-this-token" not in audit.evidence_json
        receipt = audit.model_dump()
    sweep(evidence)
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).model_dump() == receipt


@pytest.mark.parametrize("tier", ["kg", "project", "interactive"])
def test_workflow_died_before_hold_settles_non_probe_guest(database, monkeypatch, tier):
    """The drainer shape here is the one where the hold was never written.

    swarm/drainer.py calls hold_drainer_job on InvocationOutcomeUnknown, so a
    drainer permit normally arrives with its routine job parked. When the
    workflow dies before that call the row agent/routine_jobs.py last wrote
    still says last_status ok with next_run_at set, which is what seed writes
    unless job_held is passed. Interactive permits have no routine job at all.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, tier, tier=tier)
    sweep(proof(f"guest-{tier}"))
    assert before(database, pid)[0]["state"] == "settled"
    assert before(database, pid)[0]["outcome"] == "guest_cessation_confirmed"


def test_factory_owned_session_is_skipped(database, monkeypatch):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(
        database,
        "factory",
        tier="project",
        workflow_id="factory-workflow",
        node_key="implement",
        routine_job_name=None,
        local_session_id="factory:task-1:implement:1",
    )
    assert pid not in supervision._candidates()
    original = before(database, pid)
    sweep(proof("guest-factory"))
    assert before(database, pid) == original


def test_factory_owned_session_is_skipped_via_swarm_node_run_pin(database, monkeypatch):
    """A drainer-shaped permit still gets excluded when it carries a pin.

    local_session_id and routine_job_name here are indistinguishable from a
    real drainer row, so only the SwarmNodeRun.pin_json ownership check can
    tell this attempt is factory-owned.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(
        database,
        "factory-pin",
        tier="project",
        workflow_id="wf-factory-pin",
        node_key="qwen-drain",
        routine_job_name="job:factory-pin",
    )
    with Session(database) as db, db.begin():
        permit = db.get(AgentCapacityReservation, pid)
        db.add(SwarmTask(id="task-pin", task_text="pinned", conductor_model="luna"))
        db.add(
            SwarmNodeRun(
                task_id="task-pin",
                node_key="implement",
                attempt=1,
                pin_json="{}",
                session_id=permit.session_id,
                status="dispatched",
            )
        )
    assert pid not in supervision._candidates()
    original = before(database, pid)
    sweep(proof("guest-factory-pin"))
    assert before(database, pid) == original


@pytest.mark.parametrize("tier", ["probe", "kg", "project", "interactive"])
def test_no_guest_without_delivery_settles(database, monkeypatch, tier):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(
        database,
        f"no-guest-{tier}",
        tier=tier,
        guest_bound=False,
        dispatch_count=2,
        claimed=True,
    )
    sweep(None)
    assert before(database, pid)[0]["state"] == "settled"
    assert before(database, pid)[0]["outcome"] == "no_guest_bound"


def test_no_guest_without_claim_identity_remains_uncertain(database, monkeypatch):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, "unclaimed", tier="kg", guest_bound=False, claimed=False)
    original = before(database, pid)
    sweep(None)
    assert before(database, pid) == original


@pytest.mark.parametrize("guest_bound", [False, True])
def test_pending_message_blocks_settlement(database, monkeypatch, guest_bound):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(
        database,
        f"pending-{guest_bound}",
        tier="kg",
        guest_bound=guest_bound,
        dispatch_count=1,
    )
    with Session(database) as db, db.begin():
        permit = db.get(AgentCapacityReservation, pid)
        db.add(
            PendingMessage(
                session_id=permit.session_id,
                seq=2,
                message_text="still owned by an executor",
            )
        )
    original = before(database, pid)
    sweep(proof(f"guest-pending-{guest_bound}"))
    assert before(database, pid) == original


@pytest.mark.parametrize(
    "recovery_updates",
    [None, {"guest_id": "guest-attempted"}, {"binding": {"persisted": True}}],
)
def test_no_guest_with_delivery_or_missing_recovery_remains_uncertain(
    database, monkeypatch, recovery_updates
):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(
        database,
        "attempted",
        tier="kg",
        guest_bound=False,
        dispatch_count=1,
        recovery_updates=recovery_updates,
    )
    if recovery_updates is None:
        with Session(database) as db, db.begin():
            permit = db.get(AgentCapacityReservation, pid)
            turn = db.exec(
                select(AgentTurn).where(AgentTurn.session_id == permit.session_id)
            ).one()
            turn.usage_json = "{}"
            db.add(turn)
    original = before(database, pid)
    sweep(None)
    assert before(database, pid) == original


def test_non_probe_flag_off_leaves_kg_and_project_untouched(database, monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_SUPERVISION_ENABLED", "true")
    monkeypatch.delenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", raising=False)
    probe_id = seed(database, "probe")
    other_ids = [seed(database, tier, tier=tier) for tier in ("kg", "project")]
    originals = [before(database, pid) for pid in other_ids]
    sweep(lambda guest: proof(guest))
    assert before(database, probe_id)[0]["state"] == "settled"
    assert [before(database, pid) for pid in other_ids] == originals


def test_probe_flag_off_excludes_probe_when_general_flag_is_on(database, monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_SUPERVISION_ENABLED", "false")
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    probe_id = seed(database, "probe")
    kg_id = seed(database, "kg", tier="kg")
    original_probe = before(database, probe_id)
    sweep(lambda guest: proof(guest))
    assert before(database, probe_id) == original_probe
    assert before(database, kg_id)[0]["state"] == "settled"


def test_cessation_timestamp_before_failed_turn_does_not_settle(database, monkeypatch):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, "early", tier="project")
    original = before(database, pid)
    created_at = original[2][0]["created_at"]
    updated_at = int(created_at.timestamp() * 1000) - 1
    sweep(
        proof(
            "guest-early",
            invoke_started_at=updated_at - 1000,
            last_invoke_at=updated_at - 500,
            updated_at=updated_at,
        )
    )
    assert before(database, pid) == original


def test_destroyed_with_timestamp_settles_under_general_flag(database, monkeypatch):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database)
    sweep(proof(state="destroyed"))
    assert before(database, pid)[0]["state"] == "settled"


def test_probe_only_destroyed_view_preserves_hold_and_binding(database, monkeypatch):
    monkeypatch.delenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", raising=False)
    pid = seed(database)
    original = before(database, pid)
    sweep(proof(state="destroyed"))
    assert before(database, pid) == original


@pytest.mark.parametrize("tier", ["kg", "project"])
def test_held_routine_job_permit_is_left_to_the_operator_path(
    database, monkeypatch, tier
):
    """Production shape, and the ordinary one: swarm/drainer.py calls
    hold_drainer_job on every InvocationOutcomeUnknown, and agent/routine_jobs.py
    hold_job_for_unknown_outcome writes the row this seeds: last_status
    unknown_invocation, next_run_at NULL, a "session_id=<id>: " summary and no
    lock holder. Only agent/routine_reconciliation.py re-arms that row, and its
    delivery_error_hold predicate requires the reservation to still be
    uncertain, so settling here would strand the job forever. The operator path
    owns both supported drainer kinds, and settling here would remove its exact
    permit evidence before it can apply the requested job disposition.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, f"held-{tier}", tier=tier, job_held=True)
    assert pid in supervision._candidates()
    with Session(database) as db:
        row = db.execute(
            text(
                "SELECT routine_kind, last_status, next_run_at, last_summary "
                "FROM routine_jobs"
            )
        ).one()
        assert row.routine_kind == ("kg-drain" if tier == "kg" else "qwen-drain")
        assert row.last_status == UNKNOWN_INVOCATION
        assert row.next_run_at is None
        permit = db.get(AgentCapacityReservation, pid)
        assert row.last_summary.startswith(f"session_id={permit.session_id}:")
    original = before(database, pid)
    sweep(proof(f"guest-held-{tier}"))
    assert before(database, pid) == original
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).reason == "routine_job_held"


def test_interactive_session_with_completed_history_settles(database, monkeypatch):
    """Production shape: agent_sessions/router.py keeps one interactive session
    per conversation and store.create_turn appends a turn per send, so the
    failed turn arrives behind completed turns 1 and 2 rather than alone. Each
    of those sends also went through admission.claim_pending, which calls
    reserve_start per turn, so the session owns three reservation rows and the
    two earlier ones are settled.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, "chat", tier="interactive", history=2)
    assert before(database, pid)[0]["pending_seq"] == 3
    with Session(database) as db:
        permit = db.get(AgentCapacityReservation, pid)
        rows = db.exec(
            select(AgentCapacityReservation)
            .where(AgentCapacityReservation.session_id == permit.session_id)
            .order_by(AgentCapacityReservation.pending_seq)
        ).all()
        assert [(r.pending_seq, r.state) for r in rows] == [
            (1, "settled"),
            (2, "settled"),
            (3, "uncertain"),
        ]
    assert pid in supervision._candidates()
    sweep(proof("guest-chat"))
    after = before(database, pid)
    assert after[0]["state"] == "settled"
    assert after[0]["outcome"] == "guest_cessation_confirmed"
    assert [turn["seq"] for turn in after[2]] == [1, 2, 3]


def test_interactive_reservation_past_the_failed_turn_is_ambiguous(
    database, monkeypatch
):
    """A row at a later seq means admission.claim_pending already reserved a
    start for a newer send (agent_sessions/admission.py claim_pending calls
    reserve_start with the new pending seq), so this permit is no longer the
    session's live attempt and the loop must not settle it.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, "chat-resent", tier="interactive", history=1)
    with Session(database) as db, db.begin():
        permit = db.get(AgentCapacityReservation, pid)
        db.add(
            AgentCapacityReservation(
                local_session_id=permit.local_session_id,
                session_id=permit.session_id,
                pending_seq=permit.pending_seq + 1,
                tier="interactive",
                state="reserved",
                owner="worker",
            )
        )
    original = before(database, pid)
    sweep(proof("guest-chat-resent"))
    assert before(database, pid) == original
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).reason == "ambiguous_ownership"


def test_drainer_history_is_reported_as_unsupported_shape(database, monkeypatch):
    """A kg drainer session is created per job by swarm/drainer.py and carries
    exactly one turn, so history on one is a shape this loop does not model.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, "kg-history", tier="kg", history=1)
    original = before(database, pid)
    sweep(proof("guest-kg-history"))
    assert before(database, pid) == original
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).reason == "unsupported_shape"


def test_later_turn_after_the_failed_turn_is_a_changed_attempt(database, monkeypatch):
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, "resent", tier="interactive", history=1)
    with Session(database) as db, db.begin():
        permit = db.get(AgentCapacityReservation, pid)
        db.add(
            AgentTurn(
                session_id=permit.session_id,
                seq=3,
                prompt="a later send",
                result_text="done",
            )
        )
    sweep(proof("guest-resent"))
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).reason == "changed_attempt"


@pytest.mark.parametrize(
    "field",
    [name for name in supervision._BINDING_EVIDENCE if name != "guest_cleanup_id"],
)
def test_residual_binding_evidence_blocks_a_no_guest_settlement(
    database, monkeypatch, field
):
    """Whatever cleared an earlier binding, these columns survive it: they are
    written by store.set_ember_session, replace_ember_session_after_preemption
    and the guest cleanup claim in agent_sessions/admission.py. The operator
    destroy that motivated this lives in admission_test.py, which can import
    store: test_operator_destroy_retains_a_binding_under_an_unknown_outcome.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(database, f"residual-{field}", tier="kg", guest_bound=False)
    with Session(database) as db, db.begin():
        permit = db.get(AgentCapacityReservation, pid)
        agent = db.get(AgentSession, permit.session_id)
        setattr(
            agent,
            field,
            datetime.now(timezone.utc) if field.endswith("_at") else "residual",
        )
        db.add(agent)
    original = before(database, pid)
    sweep(None)
    assert before(database, pid) == original
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).reason == "prior_binding_evidence"


def test_production_drainer_kg_settles_when_the_workflow_died_before_the_hold(
    database, monkeypatch
):
    """Selector coverage for the real kg row shape, again pre-hold.

    swarm/drainer.py builds the local_session_id as
    "<workflow>:<node_key>:<job_name>" (_session_key) and registers the job
    name on the reservation, which is the only selector _candidates matches on.
    The routine job row is still armed because hold_drainer_job never ran.
    """
    monkeypatch.setenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", "true")
    pid = seed(
        database,
        "production-kg",
        tier="kg",
        workflow_id="wf-1",
        node_key="kg-drain",
        routine_job_name="kg-job",
    )
    assert pid in supervision._candidates()
    sweep(proof("guest-production-kg"))
    assert before(database, pid)[0]["state"] == "settled"


@pytest.mark.parametrize(
    "state",
    [
        "running",
        "parked",
        "banked",
        "destroying",
        "expired",
        "failed",
        "EVICTED",
    ],
)
def test_only_eviction_qualifies(database, state):
    pid = seed(database)
    original = before(database, pid)
    sweep(proof(state=state))
    assert before(database, pid) == original


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "wrong"},
        {"generation": None},
        {"generation": True},
        {"invoke_started_at": None},
        {"last_invoke_at": None},
        {"last_invoke_at": -1},
        {"updated_at": 0},
        {"updated_at": 99999999999999},
    ],
)
def test_bad_proof_preserves_hold(database, change):
    pid = seed(database)
    original = before(database, pid)
    sweep(proof(**change))
    assert before(database, pid) == original


@pytest.mark.parametrize(
    "response", [None, [], {}, RuntimeError("HTTP 404"), TimeoutError()]
)
def test_unavailable_or_malformed_is_not_cessation(database, response):
    pid = seed(database)
    original = before(database, pid)
    sweep(response)
    assert before(database, pid) == original


def test_banking_generation_advance_survives_observer_restart(database):
    pid = seed(database)
    observed = proof(state="running")
    sweep(observed)
    # A new sweep/new event loop reads the durable first observation. Banking
    # advances generation, while the owned invocation timestamp stays fixed.
    observed.update(
        state="evicted", generation=1, updated_at=observed["updated_at"] + 1
    )
    sweep(observed)
    assert before(database, pid)[0]["state"] == "settled"


@pytest.mark.parametrize("field", ["generation", "invoke_started_at", "updated_at"])
def test_reordered_or_different_invocation_refuses(database, field):
    pid = seed(database)
    observed = proof(state="running", generation=2)
    sweep(observed)
    observed.update(state="evicted")
    observed[field] -= 1
    sweep(observed)
    assert before(database, pid)[0]["state"] == "uncertain"


def test_new_invoke_after_failed_turn_refuses_even_first_observation(database):
    pid = seed(database)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    sweep(proof(invoke_started_at=now - 3000, last_invoke_at=now - 2000))
    assert before(database, pid)[0]["state"] == "uncertain"


@pytest.mark.parametrize(
    "mutation", ["pending", "turn", "binding", "outcome", "duplicate_owner"]
)
def test_ownership_changes_during_get_refuse(database, mutation):
    pid = seed(database)
    candidate = supervision._prepare(pid)
    with Session(database) as db, db.begin():
        permit = db.get(AgentCapacityReservation, pid)
        agent = db.get(AgentSession, permit.session_id)
        if mutation == "pending":
            db.add(PendingMessage(session_id=agent.id, seq=2, message_text="new work"))
        elif mutation == "turn":
            db.add(
                AgentTurn(session_id=agent.id, seq=2, prompt="new", result_text="done")
            )
        elif mutation == "binding":
            agent.ember_session_id = "new-guest"
        elif mutation == "outcome":
            permit.outcome = "different"
        else:
            db.add(
                AgentSession(
                    local_session_id="other",
                    workspace="<guest>",
                    branch="main",
                    ember_session_id=agent.ember_session_id,
                )
            )
    original = before(database, pid)
    supervision._record(candidate, proof(), datetime.now(timezone.utc))
    assert before(database, pid) == original


def test_commit_and_duplicate_observation_are_atomic(database, monkeypatch):
    pid = seed(database)
    original = before(database, pid)
    candidate = supervision._prepare(pid)
    real = admission.confirm_guest_cessation

    def fail_after_mutation(db, agent):
        real(db, agent)
        raise RuntimeError("injected failure before commit")

    monkeypatch.setattr(admission, "confirm_guest_cessation", fail_after_mutation)
    with pytest.raises(RuntimeError, match="injected"):
        supervision._record(candidate, proof(), datetime.now(timezone.utc))
    assert before(database, pid) == original
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).settled_at is None
    monkeypatch.setattr(admission, "confirm_guest_cessation", real)
    observed = proof()
    supervision._record(candidate, observed, datetime.now(timezone.utc))
    first = before(database, pid)
    supervision._record(candidate, observed, datetime.now(timezone.utc))
    assert before(database, pid) == first


def test_expired_proof_after_lock_wait_preserves_hold(database):
    pid = seed(database)
    candidate = supervision._prepare(pid)
    supervision._record(
        candidate, proof(), datetime.now(timezone.utc) - timedelta(seconds=31)
    )
    assert before(database, pid)[0]["state"] == "uncertain"


def test_batch_fairness_and_failed_read_do_not_starve_other_probes(database):
    ids = [seed(database, str(n)) for n in range(6)]
    visits = []
    evidence = {f"guest-{n}": proof(f"guest-{n}", state="running") for n in range(6)}

    def get(guest):
        visits.append(guest)
        if guest == "guest-0":
            raise RuntimeError("unreachable")
        return evidence[guest]

    sweep(get)
    assert len(visits) == supervision.BATCH_SIZE
    sweep(get)
    assert {"guest-4", "guest-5"}.issubset(visits)
    for observed in evidence.values():
        observed["state"] = "evicted"
    sweep(get)
    assert any(before(database, p)[0]["state"] == "settled" for p in ids)


def test_failed_get_does_not_block_later_terminal_candidate(database):
    first = seed(database, "first")
    second = seed(database, "second")

    def get(guest):
        if guest == "guest-first":
            raise RuntimeError("unreachable")
        return proof(guest)

    sweep(get)
    assert before(database, first)[0]["state"] == "uncertain"
    assert before(database, second)[0]["state"] == "settled"


def test_disabled_start_creates_no_task(monkeypatch):
    monkeypatch.delenv("AGENT_PROBE_SUPERVISION_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_UNCERTAIN_PERMIT_SUPERVISION_ENABLED", raising=False)
    assert supervision.start_permit_supervision_loop() == []


def test_enabled_leader_task_is_cancellable(monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_SUPERVISION_ENABLED", "true")

    async def check():
        tasks = supervision.start_permit_supervision_loop()
        assert len(tasks) == 1
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]

    asyncio.run(check())


def test_reimporting_maintenance_keeps_one_model_registration():
    import importlib

    original = ProbeObservation.__table__
    importlib.reload(supervision)
    assert supervision.ProbeObservation.__table__ is original


def test_nonsettling_reasons_are_visible_without_secret_payloads(database, caplog):
    pid = seed(database)
    with caplog.at_level("INFO", logger=supervision.__name__):
        sweep(proof(state="running", unexpected="never-log-this"))
    assert f"Permit supervision permit {pid}: awaiting_cessation" in caplog.text
    assert "never-log-this" not in caplog.text
    assert "never-export-this-token" not in caplog.text
