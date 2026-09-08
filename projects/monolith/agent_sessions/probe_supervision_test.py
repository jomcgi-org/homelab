import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import admission, probe_supervision as supervision
from agent_sessions.constants import UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from agent_sessions.probe_supervision import ProbeObservation


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'probes.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={"schema_translate_map": {"agent_sessions": None}},
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
            )
        ],
    )
    monkeypatch.setattr(supervision, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def seed(engine, key="one", tier="probe", legacy=False):
    at = datetime.now(timezone.utc) - timedelta(seconds=10)
    with Session(engine) as db, db.begin():
        agent = AgentSession(
            local_session_id="synthetic:" + key,
            workspace="<guest>",
            branch="main",
            admission_tier=tier,
            status="warn" if legacy else "failed",
            ember_session_id="guest-" + key,
            ember_session_token="never-export-this-token",
            last_turn_at=at,
        )
        db.add(agent)
        db.flush()
        permit = AgentCapacityReservation(
            local_session_id=agent.local_session_id,
            session_id=agent.id,
            pending_seq=1,
            tier=tier,
            state="uncertain",
            outcome="delivery_error" if legacy else "executor_cancelled",
        )
        db.add(permit)
        db.add(
            AgentTurn(
                session_id=agent.id,
                seq=1,
                prompt="original prompt",
                result_text="lost",
                terminal_reason="error",
                stop_reason=None if legacy else UNKNOWN_INVOCATION,
                cost_usd=None,
                created_at=at,
                artifact_blob=b"retained artifact",
            )
        )
        db.flush()
        return permit.id


def before(engine, permit_id):
    with Session(engine) as db:
        permit = db.get(AgentCapacityReservation, permit_id)
        agent = db.get(AgentSession, permit.session_id)
        turns = db.exec(select(AgentTurn).where(AgentTurn.session_id == agent.id)).all()
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
    assert after[1:] == original[1:]
    assert [before(database, p) for p in others] == untouched
    with Session(database) as db:
        audit = db.get(ProbeObservation, pid)
        assert audit.settled_at is not None
        assert "never-export-this-token" not in audit.evidence_json
        receipt = audit.model_dump()
    sweep(evidence)
    with Session(database) as db:
        assert db.get(ProbeObservation, pid).model_dump() == receipt


@pytest.mark.parametrize(
    "state",
    [
        "running",
        "parked",
        "banked",
        "destroying",
        "destroyed",
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
    assert supervision.start_probe_supervision_loop() == []


def test_enabled_leader_task_is_cancellable(monkeypatch):
    monkeypatch.setenv("AGENT_PROBE_SUPERVISION_ENABLED", "true")

    async def check():
        tasks = supervision.start_probe_supervision_loop()
        assert len(tasks) == 1
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]

    asyncio.run(check())
