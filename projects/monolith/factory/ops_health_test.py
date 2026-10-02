"""Alerting health components: definitions, data reads, caching, no-raise."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import core.db
import pytest
from core.platform_probe import PlatformProbe
from knowledge.models import Dispute
from sqlmodel import Session, SQLModel, create_engine

from factory import ops_health
from factory.execution import create_outcome
from factory.execution.models import AgentSession, AgentTurn
from factory.orchestration.factory_models import (
    FactoryControl,
    FactoryReceipt,
    WorkItem,
)
from factory.orchestration.models import SwarmTask

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ops.db'}",
        connect_args={"check_same_thread": False},
        execution_options={
            "schema_translate_map": {
                "swarm": None,
                "agent_sessions": None,
                "knowledge": None,
            }
        },
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                PlatformProbe,
                AgentSession,
                AgentTurn,
                SwarmTask,
                WorkItem,
                FactoryControl,
                FactoryReceipt,
                Dispute,
            )
        ],
    )
    monkeypatch.setattr(core.db, "get_engine", lambda: engine)
    ops_health.reset_caches()
    yield engine
    ops_health.reset_caches()


# --- kg_dispute_resolution --------------------------------------------------


def test_dispute_resolution_evaluate_reports_count_and_caps_note_ids():
    note_ids = [f"note-{i}" for i in range(8)]
    result = ops_health.evaluate_kg_dispute_resolution(9, note_ids)
    assert result["ok"] is False
    assert "9 dispute resolution(s) failed" in result["detail"]
    assert "note-0" in result["detail"] and "note-4" in result["detail"]
    assert "note-5" not in result["detail"]
    assert ops_health.evaluate_kg_dispute_resolution(0, [])["ok"] is True


def test_dispute_resolution_reader_filters_window_and_state(engine, monkeypatch):
    monkeypatch.setattr(ops_health, "_now", lambda: NOW)
    boundary = NOW - ops_health.KG_DISPUTE_FAILURE_WINDOW
    with Session(engine) as session:
        session.add_all(
            [
                Dispute(
                    note_id="recent",
                    reason="wrong",
                    state="resolution_failed",
                    resolved_at=NOW,
                ),
                Dispute(
                    note_id="boundary",
                    reason="wrong",
                    state="resolution_failed",
                    resolved_at=boundary,
                ),
                Dispute(
                    note_id="old",
                    reason="wrong",
                    state="resolution_failed",
                    resolved_at=boundary - timedelta(seconds=1),
                ),
                Dispute(
                    note_id="answered",
                    reason="wrong",
                    state="rejected",
                    resolved_at=NOW,
                ),
                Dispute(note_id="open", reason="wrong"),
            ]
        )
        session.commit()

    assert ops_health._kg_dispute_resolution_rows_sync(boundary) == (
        2,
        ["boundary", "recent"],
    )
    result = asyncio.run(ops_health.kg_dispute_resolution_health())
    assert result["ok"] is False
    assert "2 dispute resolution(s)" in result["detail"]
    assert "boundary" in result["detail"] and "recent" in result["detail"]
    assert "old" not in result["detail"] and "answered" not in result["detail"]


@pytest.mark.parametrize("age", [None, timedelta(hours=24, seconds=1)])
def test_dispute_resolution_reader_old_or_absent_is_healthy(engine, monkeypatch, age):
    monkeypatch.setattr(ops_health, "_now", lambda: NOW)
    if age is not None:
        with Session(engine) as session:
            session.add(
                Dispute(
                    note_id="old",
                    reason="wrong",
                    state="resolution_failed",
                    resolved_at=NOW - age,
                )
            )
            session.commit()
    assert asyncio.run(ops_health.kg_dispute_resolution_health())["ok"] is True


def test_dispute_resolution_reader_caps_identifiers_not_count(engine):
    with Session(engine) as session:
        session.add_all(
            [
                Dispute(
                    note_id=f"note-{i}",
                    reason="wrong",
                    state="resolution_failed",
                    resolved_at=NOW,
                )
                for i in range(8)
            ]
        )
        session.commit()
    count, note_ids = ops_health._kg_dispute_resolution_rows_sync(
        NOW - timedelta(hours=24)
    )
    assert count == 8
    assert note_ids == [f"note-{i}" for i in range(5)]


def test_dispute_resolution_check_registered_and_reader_error_is_unknown(monkeypatch):
    def unreadable(_since):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ops_health, "_kg_dispute_resolution_rows_sync", unreadable)
    assert (
        ops_health.CHECKS["kg_dispute_resolution"]
        is ops_health.kg_dispute_resolution_health
    )
    result = asyncio.run(ops_health.kg_dispute_resolution_health())
    assert result["status"] == "unknown"
    assert result["ok"] is True


# --- embervm_capacity -------------------------------------------------------


def _probe(ok, checked_at, last_ok_at=None, detail="x"):
    return SimpleNamespace(
        ok=ok, checked_at=checked_at, last_ok_at=last_ok_at, detail=detail
    )


def test_create_streak_needs_failures_spanning_fifteen_minutes():
    start = NOW - timedelta(minutes=20)
    since = _probe(False, start)
    # One failure 20 minutes ago and nothing since: not continuous.
    failing, _ = ops_health.evaluate_create_streak(_probe(False, start), since, NOW)
    assert not failing
    # Failures from 20 minutes ago through 2 minutes ago: continuous.
    latest = _probe(False, NOW - timedelta(minutes=2), detail="429 no_capacity")
    failing, detail = ops_health.evaluate_create_streak(latest, since, NOW)
    assert failing
    assert "429 no_capacity" in detail and "20m" in detail
    # Ten minutes of failures stays under the threshold.
    failing, _ = ops_health.evaluate_create_streak(
        latest, _probe(False, NOW - timedelta(minutes=10)), NOW
    )
    assert not failing


def test_create_streak_ignores_a_streak_row_older_than_the_last_success():
    latest = _probe(
        False, NOW - timedelta(minutes=1), last_ok_at=NOW - timedelta(minutes=5)
    )
    stale = _probe(False, NOW - timedelta(hours=3))
    failing, _ = ops_health.evaluate_create_streak(latest, stale, NOW)
    assert not failing


def test_create_streak_success_and_empty_are_healthy():
    assert not ops_health.evaluate_create_streak(None, None, NOW)[0]
    assert not ops_health.evaluate_create_streak(_probe(True, NOW), None, NOW)[0]


def test_recorder_writes_the_streak_start_only_on_the_flip(engine):
    t0 = NOW - timedelta(minutes=30)
    create_outcome._record_sync(True, "created", now=t0)
    create_outcome._record_sync(False, "429 no_capacity", now=t0 + timedelta(minutes=1))
    create_outcome._record_sync(False, "503", now=t0 + timedelta(minutes=20))
    with Session(engine) as s:
        latest = s.get(PlatformProbe, create_outcome.LATEST)
        since = s.get(PlatformProbe, create_outcome.FAILING_SINCE)
        failing, detail = ops_health.evaluate_create_streak(latest, since, NOW)
    assert failing
    assert "503" in detail
    create_outcome._record_sync(True, "created", now=NOW)
    with Session(engine) as s:
        latest = s.get(PlatformProbe, create_outcome.LATEST)
        since = s.get(PlatformProbe, create_outcome.FAILING_SINCE)
        assert not ops_health.evaluate_create_streak(latest, since, NOW)[0]


def test_recorder_is_off_unless_enabled(monkeypatch):
    monkeypatch.delenv(create_outcome.ENABLED_ENV, raising=False)
    calls = []
    monkeypatch.setattr(create_outcome, "_record_sync", lambda *a: calls.append(a))

    async def run():
        create_outcome.record(False, "x")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert calls == []


def test_recorder_never_raises_when_the_write_fails(monkeypatch):
    monkeypatch.setenv(create_outcome.ENABLED_ENV, "true")

    def boom(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(create_outcome, "_record_sync", boom)

    async def run():
        create_outcome.record(False, "x")
        await asyncio.gather(*list(create_outcome._pending), return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert not create_outcome._pending


def _bricks(monkeypatch, replicas):
    async def fake():
        return replicas, "" if replicas is not None else "unreadable"

    monkeypatch.setattr(ops_health, "_brick_replicas", fake)


def test_capacity_unhealthy_when_every_brick_class_is_zero(engine, monkeypatch):
    _bricks(monkeypatch, {"8gi": 0, "16gi": 0})
    result = asyncio.run(ops_health.embervm_capacity_health())
    assert result == {
        "ok": False,
        "detail": "every brick class at 0 replicas (16gi=0, 8gi=0)",
    }


def test_capacity_healthy_with_one_brick(engine, monkeypatch):
    _bricks(monkeypatch, {"8gi": 1, "16gi": 0})
    result = asyncio.run(ops_health.embervm_capacity_health())
    assert result["ok"]
    assert "8gi=1" in result["detail"]


def test_capacity_unhealthy_on_sustained_create_failures(engine, monkeypatch):
    _bricks(monkeypatch, {"8gi": 1})
    now = datetime.now(timezone.utc)
    create_outcome._record_sync(
        False, "429 no_capacity", now=now - timedelta(minutes=18)
    )
    create_outcome._record_sync(
        False, "429 no_capacity", now=now - timedelta(minutes=1)
    )
    result = asyncio.run(ops_health.embervm_capacity_health())
    assert not result["ok"]
    assert result["detail"].startswith("session creates failing since")


def test_unreadable_bricks_are_not_a_failure(engine, monkeypatch):
    monkeypatch.setenv(ops_health.BRICK_NAMESPACE_ENV, "embervm")
    # Outside a cluster load_incluster_config raises: unknown, not unhealthy.
    result = asyncio.run(ops_health.embervm_capacity_health())
    assert result["ok"]
    assert "unreadable" in result["detail"]


# --- agent_turns ------------------------------------------------------------


def _turns(engine, *turns, session_model=None):
    with Session(engine) as s:
        row = AgentSession(
            local_session_id=f"s-{session_model}",
            workspace="w",
            branch="b",
            repo="r",
            model=session_model,
        )
        s.add(row)
        s.commit()
        for seq, (model, terminal, stop, created) in enumerate(turns, start=1):
            s.add(
                AgentTurn(
                    session_id=row.id,
                    seq=seq,
                    prompt="p",
                    result_text="r",
                    model=model,
                    terminal_reason=terminal,
                    stop_reason=stop,
                    created_at=created,
                )
            )
        s.commit()


def test_turns_unhealthy_when_a_family_delivers_nothing(engine):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(minutes=10)
    _turns(
        engine,
        ("sol", "error", None, recent),
        ("luna", "error", "invocation_outcome_unknown", recent),
        # Interrupted and never-dispatched turns are not attempts.
        ("sol", "interrupted", "response_lost", recent),
        ("sol", "error", "cancelled_before_dispatch", recent),
        # Outside the window.
        ("sol", "completed", None, now - timedelta(hours=2)),
    )
    _turns(engine, (None, "completed", None, recent), session_model="opus")
    result = asyncio.run(ops_health.agent_turns_health())
    assert result["ok"] is False
    assert result["detail"] == (
        "no codex turn delivered in 60m: codex 0/2 delivered "
        "(last: error/invocation_outcome_unknown); claude 1/1 delivered in 60m"
    )


def test_turns_single_failure_and_unknown_models_do_not_alert(engine):
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    _turns(
        engine,
        ("opus", "error", None, recent),
        ("muse-spark-1.3-contributor", "error", None, recent),
        ("spark", "error", None, recent),
    )
    result = asyncio.run(ops_health.agent_turns_health())
    assert result == {
        "ok": True,
        "detail": "codex 0/0 delivered; claude 0/1 delivered in 60m",
    }


def test_turns_one_delivery_keeps_a_family_healthy():
    rows = [("terra", None, "error", None, NOW)] * 5 + [
        ("terra", None, "end_turn", None, NOW)
    ]
    stats = ops_health.summarise_turns(rows)
    assert ops_health.evaluate_turns(stats, 2)["ok"]


# --- codex_quota_fresh ------------------------------------------------------


def _quota(age, observed=True):
    return {
        "available": True,
        "providers": {"codex": {"observed": observed, "age_seconds": age}},
    }


def test_stale_codex_quota_alerts_only_with_codex_demand():
    stale = _quota(2 * 3600)
    assert ops_health.evaluate_codex_quota(stale, 3) == {
        "ok": False,
        "detail": "Codex quota observed 120m ago (limit 60m); "
        "3 Codex turn(s) attempted in 60m",
    }
    idle = ops_health.evaluate_codex_quota(stale, 0)
    assert idle["ok"] and "no Codex demand" in idle["detail"]


def test_fresh_unobserved_and_unavailable_quota():
    assert ops_health.evaluate_codex_quota(_quota(30), 5)["ok"]
    assert not ops_health.evaluate_codex_quota(_quota(None, observed=False), 1)["ok"]
    down = {"available": False, "reason": "broker unavailable", "providers": {}}
    assert not ops_health.evaluate_codex_quota(down, 1)["ok"]
    assert ops_health.evaluate_codex_quota(down, 0)["ok"]


def test_codex_quota_check_reads_broker_and_turns(engine, monkeypatch):
    from factory.execution import provider_quota

    async def fake_quota(**_):
        return _quota(4000)

    monkeypatch.setattr(provider_quota, "fetch_provider_quota", fake_quota)
    recent = datetime.now(timezone.utc) - timedelta(minutes=3)
    _turns(engine, ("sol", "completed", None, recent))
    result = asyncio.run(ops_health.codex_quota_fresh_health())
    assert result["ok"] is False
    assert "1 Codex turn(s)" in result["detail"]


# --- factory_stuck ----------------------------------------------------------

POLICY = {
    "repo": "jomcgi-org/homelab",
    "generation": 13,
    "issue_numbers": [4361],
    "intake": {"enabled": True},
}


def _receipt(
    s, rid, issue, state, *, actor="factory:intake", generation=13, updated=NOW
):
    s.add(
        FactoryReceipt(
            id=rid,
            repo="jomcgi-org/homelab",
            issue_number=issue,
            generation=generation,
            title="t",
            body="b",
            url="u",
            actor=actor,
            state=state,
            created_at=updated,
            updated_at=updated,
        )
    )


def test_factory_stuck_flags_old_uncertain_and_ineligible_queued(engine, monkeypatch):
    now = datetime.now(timezone.utc)
    with Session(engine) as s:
        s.add(
            FactoryControl(
                id="factory",
                state="enabled",
                actor="op",
                policy_json=json.dumps(POLICY),
            )
        )
        _receipt(s, 692, 6499, "uncertain", updated=now - timedelta(hours=3))
        _receipt(s, 693, 6500, "uncertain", updated=now - timedelta(minutes=30))
        # The #6483 trap: an operator receipt outside the allowlist.
        _receipt(s, 682, 6462, "queued", actor="operator-hash")
        # Admissible: allowlisted, or authored by intake while intake is on.
        _receipt(s, 684, 4361, "queued", actor="operator-hash")
        _receipt(s, 685, 6001, "queued")
        # An older generation is stranded by design, not stuck.
        _receipt(s, 672, 6442, "queued", actor="operator-hash", generation=12)
        s.commit()
    result = asyncio.run(ops_health.factory_stuck_health())
    assert result["ok"] is False
    assert result["detail"] == (
        "1 receipt(s) uncertain over 120m: receipt 692 (#6499) 180m; "
        "1 queued receipt(s) not admission-eligible: "
        "receipt 682 (#6462, actor operator-hash)"
    )


def test_factory_intake_off_is_a_pause_not_stuck():
    policy = dict(POLICY, intake={"enabled": False})
    queued = [(685, 6001, "factory:intake", "jomcgi-org/homelab", 13)]
    result = ops_health.evaluate_factory(json.dumps(policy), [], queued, NOW)
    assert result["ok"]
    assert result["detail"] == "0 uncertain (none over 120m), 1 queued"


def test_factory_without_policy_checks_uncertain_only():
    result = ops_health.evaluate_factory(None, [(1, 2, NOW)], [], NOW)
    assert result["ok"]
    assert "no factory policy" in result["detail"]


# --- caching and never raising ---------------------------------------------


def test_checks_are_cached_and_never_raise():
    calls = []

    async def boom():
        calls.append(1)
        raise RuntimeError("kaput")

    check = ops_health._CachedCheck("boom", boom)

    async def run():
        return await check(), await check()

    first, second = asyncio.run(run())
    assert first == second
    assert first["ok"] is True and first["status"] == "unknown"
    assert "kaput" in first["detail"]
    assert calls == [1]


def test_checks_are_time_bounded():
    async def slow():
        await asyncio.sleep(5)
        return {"ok": False, "detail": "never"}

    check = ops_health._CachedCheck("slow", slow, timeout_s=0.01)
    result = asyncio.run(check())
    assert result["status"] == "unknown"
    assert "timed out" in result["detail"]
