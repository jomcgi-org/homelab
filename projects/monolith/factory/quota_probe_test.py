"""Probe cadence uses real observations and survives process boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from factory import quota_probe as probe
from factory.execution.models import (
    AgentSession,
    AgentCapacityReservation,
    PendingMessage,
    AgentTurn,
    AgentResultReceipt,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmTask

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'probe.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                SwarmTask,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryAudit,
                AgentSession,
                AgentCapacityReservation,
                PendingMessage,
                AgentTurn,
                AgentResultReceipt,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="test"))
        session.commit()
    monkeypatch.setattr(probe.controls, "get_engine", lambda: engine)
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW)
    monkeypatch.setattr(
        FactoryAudit.model_fields["created_at"], "default_factory", lambda: NOW
    )
    yield engine
    engine.dispose()


def payload(age=0, observed=True):
    return {
        "available": True,
        "providers": {
            "claude": {
                "observed": observed,
                "age_seconds": age,
                "windows": [{"name": "7d", "used_percent": 11}],
            }
        },
    }


def active(db, paused=False):
    with Session(db) as session:
        session.add(
            FactoryReceipt(
                repo="test",
                issue_number=1,
                title="test",
                body="",
                url="test",
                actor="test",
                state="admitted",
                task_paused=paused,
            )
        )
        session.commit()


@pytest.mark.parametrize(
    "busy,age,expected",
    [(False, 3599, False), (False, 3600, True), (True, 899, False), (True, 900, True)],
)
def test_cadence(db, busy, age, expected):
    if busy:
        active(db)
    assert bool(probe.claim(payload(age))) is expected


def test_paused_work_uses_hourly_cadence(db):
    active(db, paused=True)
    assert probe.claim(payload(900)) is None


@pytest.mark.parametrize("age", [None, True, -1, float("nan"), float("inf"), "10"])
def test_invalid_age_cannot_suppress_probe(db, age):
    assert probe.claim(payload(age))


def test_missing_observation_probes_but_broken_broker_does_not(db):
    assert probe.claim({"available": False}) is None
    assert probe.claim(payload(observed=False))


def test_known_expired_window_does_not_count_as_fresh(db):
    reading = payload()
    reading["providers"]["claude"]["windows"][0]["expired"] = True
    assert probe.claim(reading)


def test_persistent_attempt_limits_retry_even_without_observation(db, monkeypatch):
    assert probe.claim(payload(observed=False))
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW + timedelta(seconds=3599))
    assert probe.claim(payload(observed=False)) is None
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW + timedelta(seconds=3600))
    assert probe.claim(payload(observed=False))


def test_concurrent_replicas_reserve_only_once(db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        keys = list(pool.map(lambda _: probe.claim(payload(observed=False)), range(2)))
    assert sum(key is not None for key in keys) == 1


@pytest.mark.parametrize(
    "status,ember,pending",
    [("running", None, False), ("warning", "guest", False), ("warning", None, True)],
)
def test_unresolved_guest_blocks_new_probe(db, status, ember, pending):
    with Session(db) as session:
        row = AgentSession(
            local_session_id=probe.PREFIX + "old",
            workspace="<guest>",
            branch="main",
            status=status,
            ember_session_id=ember,
        )
        session.add(row)
        session.flush()
        if pending:
            session.add(PendingMessage(session_id=row.id, seq=1, message_text="OK"))
        session.commit()
    assert probe.claim(payload(observed=False)) is None


def test_completed_guest_does_not_block(db):
    with Session(db) as session:
        session.add(
            AgentSession(
                local_session_id=probe.PREFIX + "old",
                workspace="<guest>",
                branch="main",
                status="idle",
            )
        )
        session.commit()
    assert probe.claim(payload(observed=False))


@pytest.mark.parametrize("outcome", ["observed", "no_observation", "failed"])
def test_probe_requires_broker_evidence_not_model_reply(db, monkeypatch, outcome):
    from factory.execution import execution_api, provider_quota

    calls = []

    async def fetch(**kwargs):
        return payload(observed=bool(calls) and outcome == "observed")

    async def run(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if outcome == "failed":
            raise RuntimeError("failed")
        return "quota is zero"  # Never trust model prose as telemetry.

    async def sleep(_):
        pass

    monkeypatch.setattr(provider_quota, "fetch_provider_quota", fetch)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    monkeypatch.setattr(probe.asyncio, "sleep", sleep)
    asyncio.run(probe.tick())
    assert len(calls) == 1
    assert calls[0][1]["model"] == "haiku"
    assert calls[0][1]["read_timeout"] == 120
    assert calls[0][1]["session_key"].startswith(probe.PREFIX)
    with Session(db) as session:
        actions = session.exec(
            select(FactoryAudit.action).order_by(FactoryAudit.id)
        ).all()
    assert actions == ["quota_probe_started", "quota_probe_" + outcome]


def test_total_deadline_cancels_stalled_creation(db, monkeypatch):
    from factory.execution import execution_api, provider_quota

    cancelled = []

    async def fetch(**kwargs):
        return payload(observed=False)

    async def run(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(provider_quota, "fetch_provider_quota", fetch)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    monkeypatch.setattr(probe, "TURN_TIMEOUT_SECONDS", 0.01)
    asyncio.run(probe.tick())
    assert cancelled == [True]
    with Session(db) as session:
        assert session.exec(
            select(FactoryAudit.action).order_by(FactoryAudit.id)
        ).all() == [
            "quota_probe_started",
            "quota_probe_failed",
        ]
    assert probe.claim(payload(observed=False)) is None


def codex_grant(
    age, used=10.0, exhausted=False, observed=True, windows=None, resets_at=None
):
    if windows is None:
        window = {"name": "primary", "used_percent": used}
        if resets_at is not None:
            window["resets_at"] = resets_at
        windows = [window]
    return {
        "provider": "codex",
        "observed": observed,
        "exhausted": exhausted,
        "status": "ok",
        "age_seconds": age,
        "windows": windows,
    }


def codex_payload(grants, available=True):
    base = payload(observed=False)
    base["available"] = available
    base["grants"] = grants
    base["grants_complete"] = True
    base["grants_valid"] = True
    return base


@pytest.fixture
def luna_model(monkeypatch):
    monkeypatch.setattr(probe, "_codex_probe_model", lambda: "luna")


def test_codex_probe_fires_into_all_stale_pool(db, luna_model):
    grants = {
        "codex-b": codex_grant(130422.0),
        "codex-cluster": codex_grant(3062.0),
    }
    key = probe.codex_claim(codex_payload(grants))
    assert key is not None
    assert key.startswith(probe.CODEX_PREFIX)


def test_codex_probe_skips_fresh_grant(db, luna_model):
    grants = {
        "codex-b": codex_grant(130422.0),
        "codex-cluster": codex_grant(12.0),
    }
    assert probe.codex_claim(codex_payload(grants)) is None


@pytest.mark.parametrize("refreshed_grant", ["codex-b", "codex-cluster"])
def test_codex_probe_stops_after_either_broker_grant_refresh(
    db, monkeypatch, luna_model, refreshed_grant
):
    from factory.execution import provider_quota

    raw = {
        "providers": {},
        "grants_complete": True,
        "grants": {
            "codex-cluster": codex_grant(3062.0),
            "codex-b": codex_grant(172800.0),
        },
    }
    assert probe.codex_claim(provider_quota._available_result(raw)) is not None
    # Let the 900-second cadence expire so it cannot mask a missing fresh guard.
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW + timedelta(seconds=901))
    raw["grants"][refreshed_grant]["age_seconds"] = 12.0
    assert probe.codex_claim(provider_quota._available_result(raw)) is None
    with Session(db) as session:
        starts = session.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "codex_quota_probe_started"
            )
        ).all()
        assert len(starts) == 1


def test_codex_probe_skips_all_exhausted_pool(db, luna_model):
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    flagged = {
        "codex-b": codex_grant(5000.0, exhausted=True, resets_at=future),
        "codex-cluster": codex_grant(6000.0, exhausted=True, resets_at=future),
    }
    assert probe.codex_claim(codex_payload(flagged)) is None
    spent = {
        "codex-b": codex_grant(5000.0, used=99.0),
        "codex-cluster": codex_grant(6000.0, used=98.0),
    }
    assert probe.codex_claim(codex_payload(spent)) is None


def test_codex_probe_fires_when_stale_rejections_have_reset(db, luna_model):
    # Both grants 429'd, traffic stopped, and every window has since expired.
    grants = {
        "codex-b": codex_grant(20000.0, exhausted=True, windows=[]),
        "codex-cluster": codex_grant(18000.0, exhausted=True, windows=[]),
    }
    assert probe.codex_claim(codex_payload(grants)) is not None


def test_codex_probe_fires_when_rejection_reset_has_passed(db, luna_model):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    grants = {
        "codex-b": codex_grant(20000.0, used=100.0, exhausted=True, resets_at=past),
        "codex-cluster": codex_grant(
            18000.0, used=100.0, exhausted=True, resets_at=past
        ),
    }
    assert probe.codex_claim(codex_payload(grants)) is not None


def test_codex_probe_fires_for_bare_stale_rejection(db, luna_model):
    grants = {"codex-b": codex_grant(5000.0, exhausted=True, windows=[])}
    assert probe.codex_claim(codex_payload(grants)) is not None


def test_codex_probe_skips_rejection_with_future_reset(db, luna_model):
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    grants = {
        "codex-b": codex_grant(5000.0, exhausted=True, resets_at=future),
        "codex-cluster": codex_grant(6000.0, exhausted=True, resets_at=future),
    }
    assert probe.codex_claim(codex_payload(grants)) is None


def test_codex_probe_skips_broken_broker_and_unobserved_pool(db, luna_model):
    grants = {"codex-b": codex_grant(5000.0)}
    assert probe.codex_claim(codex_payload(grants, available=False)) is None
    assert probe.codex_claim(codex_payload({})) is None
    assert probe.codex_claim(payload(observed=False)) is None
    unobserved = {"codex-b": codex_grant(5000.0, observed=False)}
    assert probe.codex_claim(codex_payload(unobserved)) is None


def test_codex_probe_rate_limits_to_one_per_window(db, monkeypatch, luna_model):
    grants = {"codex-b": codex_grant(5000.0)}
    assert probe.codex_claim(codex_payload(grants)) is not None
    assert probe.codex_claim(codex_payload(grants)) is None
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW + timedelta(seconds=899))
    assert probe.codex_claim(codex_payload(grants)) is None
    monkeypatch.setattr(probe.controls, "_now", lambda: NOW + timedelta(seconds=900))
    assert probe.codex_claim(codex_payload(grants)) is not None


def test_codex_unresolved_blocks_only_codex_probe(db, luna_model):
    with Session(db) as session:
        session.add(
            AgentSession(
                local_session_id=probe.CODEX_PREFIX + "old",
                workspace="<guest>",
                branch="main",
                status="running",
            )
        )
        session.commit()
    grants = {"codex-b": codex_grant(5000.0)}
    assert probe.codex_claim(codex_payload(grants)) is None
    assert probe.claim(payload(observed=False)) is not None


def test_claude_unresolved_does_not_block_codex_probe(db, luna_model):
    with Session(db) as session:
        session.add(
            AgentSession(
                local_session_id=probe.PREFIX + "old",
                workspace="<guest>",
                branch="main",
                status="running",
            )
        )
        session.commit()
    grants = {"codex-b": codex_grant(5000.0)}
    assert probe.codex_claim(codex_payload(grants)) is not None


def test_cleanup_advances_past_held_rows_and_bounds_unheld_guests(db):
    with Session(db) as session:
        rows = [
            AgentSession(
                local_session_id=prefix + str(index),
                workspace="<guest>",
                branch="main",
                status="idle",
                ember_session_id=f"guest-{index}",
                result_receipt_fence_id="held" if index < 6 else None,
            )
            for index, prefix in enumerate(
                [probe.PREFIX, probe.REVIEW_PREFIX, probe.CODEX_PREFIX] * 4
            )
        ]
        session.add_all(rows)
        session.commit()
    # Six held rows precede six unheld rows, including a completed Codex guest.
    assert probe._cleanup_candidates() == [
        "guest-6",
        "guest-7",
        "guest-8",
        "guest-9",
        "guest-10",
    ]


@pytest.mark.parametrize("evidence", ["pending", "turn", "reservation"])
@pytest.mark.parametrize("age, blocked", [(3599, True), (3600, True), (3601, False)])
def test_codex_orphan_fence_ages_evidence(
    db, luna_model, caplog, evidence, age, blocked
):
    assert probe.CODEX_ORPHAN_FENCE_SECONDS == 3600
    assert probe.TURN_TIMEOUT_SECONDS == 120
    assert probe.CODEX_ORPHAN_FENCE_SECONDS >= 4 * 900
    with Session(db) as session:
        row = AgentSession(
            local_session_id=probe.CODEX_PREFIX + "orphan",
            workspace="<guest>",
            branch="main",
            status="idle",
            created_at=NOW - timedelta(days=3),
            last_turn_at=NOW - timedelta(days=3),
        )
        session.add(row)
        session.flush()
        created_at = NOW - timedelta(seconds=age)
        if evidence == "pending":
            item = PendingMessage(
                session_id=row.id, seq=1, message_text="OK", created_at=created_at
            )
        elif evidence == "turn":
            item = AgentTurn(
                session_id=row.id,
                seq=1,
                prompt="OK",
                result_text="",
                stop_reason="invocation_outcome_unknown",
                created_at=created_at,
            )
        else:
            item = AgentCapacityReservation(
                session_id=row.id,
                local_session_id=row.local_session_id,
                pending_seq=1,
                tier="probe",
                state="uncertain",
                created_at=created_at,
            )
        session.add(item)
        session.commit()
    with caplog.at_level("INFO", logger=probe.__name__):
        key = probe.codex_claim(codex_payload({"codex-b": codex_grant(5000.0)}))
    assert (key is None) is blocked
    reason = "pending turn" if evidence == "pending" else "unknown outcome"
    if blocked:
        assert f"Codex quota probe suppressed: session 1 has {reason}" in caplog.text
    else:
        assert "Codex quota probe suppressed" not in caplog.text


@pytest.mark.parametrize(
    "status, guest, reason",
    [
        ("idle", "guest", "bound guest"),
        ("running", None, "running"),
    ],
)
def test_codex_bound_or_running_fence_never_ages_out(
    db, luna_model, caplog, status, guest, reason
):
    with Session(db) as session:
        session.add(
            AgentSession(
                local_session_id=probe.CODEX_PREFIX + "old",
                workspace="<guest>",
                branch="main",
                status=status,
                ember_session_id=guest,
                created_at=NOW - timedelta(days=3),
                last_turn_at=NOW - timedelta(days=3),
            )
        )
        session.commit()
    with caplog.at_level("INFO", logger=probe.__name__):
        assert (
            probe.codex_claim(codex_payload({"codex-b": codex_grant(5000.0)})) is None
        )
    assert f"Codex quota probe suppressed: session 1 has {reason}" in caplog.text


def test_codex_tick_sends_unpinned_luna_probe(db, monkeypatch, luna_model):
    from factory.execution import execution_api, provider_quota

    stale = codex_payload({"codex-b": codex_grant(5000.0)})
    fresh = codex_payload({"codex-b": codex_grant(5.0)})
    calls = []

    async def fetch(**kwargs):
        return fresh if calls else stale

    async def run(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "OK"

    async def sleep(_):
        pass

    monkeypatch.setattr(provider_quota, "fetch_provider_quota", fetch)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    monkeypatch.setattr(probe.asyncio, "sleep", sleep)
    asyncio.run(probe.codex_tick())
    assert len(calls) == 1
    assert calls[0][1]["model"] == "luna"
    assert calls[0][1]["read_timeout"] == 120
    assert calls[0][1]["session_key"].startswith(probe.CODEX_PREFIX)
    with Session(db) as session:
        actions = session.exec(
            select(FactoryAudit.action).order_by(FactoryAudit.id)
        ).all()
    assert actions == [probe.CODEX_STARTED_ACTION, probe.CODEX_OBSERVED_ACTION]


def test_codex_tick_records_missing_observation(db, monkeypatch, luna_model):
    from factory.execution import execution_api, provider_quota

    stale = codex_payload({"codex-b": codex_grant(5000.0)})
    calls = []

    async def fetch(**kwargs):
        return stale

    async def run(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "OK"

    async def sleep(_):
        pass

    monkeypatch.setattr(provider_quota, "fetch_provider_quota", fetch)
    monkeypatch.setattr(execution_api, "run_synthetic_session", run)
    monkeypatch.setattr(probe.asyncio, "sleep", sleep)
    asyncio.run(probe.codex_tick())
    assert len(calls) == 1
    with Session(db) as session:
        actions = session.exec(
            select(FactoryAudit.action).order_by(FactoryAudit.id)
        ).all()
    assert actions == [probe.CODEX_STARTED_ACTION, probe.CODEX_NO_OBSERVATION_ACTION]


@pytest.mark.parametrize("prefix", [probe.PREFIX, "synthetic:factory-review:"])
def test_unconfirmed_cleanup_blocks_until_exact_guest_confirmed(
    db, monkeypatch, prefix
):
    from factory.execution import execution_api, mcp

    with Session(db) as session:
        session.add(
            AgentSession(
                local_session_id=prefix + "old",
                workspace="<guest>",
                branch="main",
                status="idle",
                ember_session_id="guest",
            )
        )
        session.commit()
    confirmations = [False, True]
    deleted = []

    async def destroy(guest):
        assert guest == "guest"
        return confirmations.pop(0)

    def clear(guest):
        deleted.append(guest)
        with Session(db) as session:
            row = session.exec(select(AgentSession)).one()
            row.ember_session_id = None
            session.add(row)
            session.commit()

    monkeypatch.setattr(execution_api, "destroy_and_confirm", destroy)
    monkeypatch.setattr(mcp, "_clear_ember_bindings_for", clear)
    asyncio.run(probe._cleanup())
    assert deleted == []
    if prefix == probe.PREFIX:
        assert probe.claim(payload(observed=False)) is None
    asyncio.run(probe._cleanup())
    assert deleted == ["guest"]
    assert probe.claim(payload(observed=False))
