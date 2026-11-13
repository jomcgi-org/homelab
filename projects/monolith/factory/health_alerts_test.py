"""Discord transition alerts: dedupe, renotify, persistence, isolation."""

import asyncio
from datetime import datetime, timedelta, timezone

import core.db
import pytest
import shared.notify
from core.platform_probe import PlatformProbe
from sqlmodel import Session, SQLModel, create_engine

from factory import health_alerts

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


class _Messages(list):
    engine = None


@pytest.fixture
def sent(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'alerts.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine, tables=[PlatformProbe.__table__])
    monkeypatch.setattr(core.db, "get_engine", lambda: engine)
    messages = _Messages()

    async def fake_notify(message, level="info", channel=None):
        messages.append((message, level, channel))
        return {"ok": True}

    monkeypatch.setattr(shared.notify, "notify", fake_notify)
    monkeypatch.delenv(health_alerts.CHANNEL_ENV, raising=False)
    monkeypatch.delenv(health_alerts.RENOTIFY_ENV, raising=False)
    messages.engine = engine
    return messages


def _check(results):
    """A check returning the next queued result each call."""

    async def check():
        return results[0]

    return check


def _cycle(checks, now):
    return asyncio.run(health_alerts.run_cycle(checks, now=now))


def test_dispute_resolution_flip_and_recovery_notify_discord(sent):
    from factory.ops_health import evaluate_kg_dispute_resolution

    state = [evaluate_kg_dispute_resolution(0, [])]
    checks = {"kg_dispute_resolution": _check(state)}
    assert _cycle(checks, T0) == []
    state[0] = evaluate_kg_dispute_resolution(1, ["contested-note"])
    assert _cycle(checks, T0 + timedelta(minutes=1)) == [
        "Health alert: kg_dispute_resolution is unhealthy. " + state[0]["detail"]
    ]
    assert _cycle(checks, T0 + timedelta(minutes=2)) == []
    state[0] = evaluate_kg_dispute_resolution(0, [])
    assert _cycle(checks, T0 + timedelta(minutes=3)) == [
        "Health recovered: kg_dispute_resolution is healthy again. "
        + state[0]["detail"]
    ]
    assert len(sent) == 2


def test_flip_recover_and_dedupe(sent):
    state = [{"ok": True, "detail": "fine"}]
    checks = {"agent_turns": _check(state)}

    assert _cycle(checks, T0) == []  # healthy first sight is baselined silently

    state[0] = {"ok": False, "detail": "no codex turn delivered in 60m"}
    assert _cycle(checks, T0 + timedelta(minutes=1)) == [
        "Health alert: agent_turns is unhealthy. no codex turn delivered in 60m"
    ]
    # Still failing a minute later: no repeat.
    assert _cycle(checks, T0 + timedelta(minutes=2)) == []

    state[0] = {"ok": True, "detail": "codex 3/3 delivered"}
    assert _cycle(checks, T0 + timedelta(minutes=3)) == [
        "Health recovered: agent_turns is healthy again. codex 3/3 delivered"
    ]
    assert _cycle(checks, T0 + timedelta(minutes=4)) == []
    assert [level for _, level, _ in sent] == ["warn", "info"]


def test_unhealthy_on_first_sight_and_renotify(sent, monkeypatch):
    monkeypatch.setenv(health_alerts.RENOTIFY_ENV, "3600")
    monkeypatch.setenv(health_alerts.CHANNEL_ENV, "123")
    checks = {"factory_stuck": _check([{"ok": False, "detail": "receipt 692"}])}
    assert len(_cycle(checks, T0)) == 1
    assert _cycle(checks, T0 + timedelta(minutes=59)) == []
    assert _cycle(checks, T0 + timedelta(minutes=61)) == [
        "Health alert: factory_stuck is still unhealthy (last notice 1h ago). receipt 692"
    ]
    assert _cycle(checks, T0 + timedelta(minutes=62)) == []
    assert {channel for _, _, channel in sent} == {"123"}


def test_state_survives_a_new_leader(sent):
    # A leader handover is a fresh process: dedupe must come from the table.
    checks = {"embervm_capacity": _check([{"ok": False, "detail": "zero bricks"}])}
    _cycle(checks, T0)
    # run_cycle holds no in-process state; every decision reads the table.
    assert _cycle(checks, T0 + timedelta(minutes=5)) == []
    with Session(sent.engine) as s:
        row = s.get(PlatformProbe, "health_alert.embervm_capacity")
        assert row.ok is False and row.detail == "zero bricks"


def test_unknown_results_never_transition(sent):
    state = [{"ok": False, "detail": "bad"}]
    checks = {"codex_quota_fresh": _check(state)}
    _cycle(checks, T0)
    state[0] = {"ok": True, "status": "unknown", "detail": "check timed out"}
    assert _cycle(checks, T0 + timedelta(minutes=1)) == []


def test_failed_enqueue_retries_next_cycle(sent, monkeypatch):
    async def down(*_a, **_k):
        raise RuntimeError("outbox down")

    monkeypatch.setattr(shared.notify, "notify", down)
    checks = {
        "agent_turns": _check([{"ok": False, "detail": "a"}]),
    }
    assert _cycle(checks, T0) == []  # logged, not raised, nothing recorded

    async def up(message, level="info", channel=None):
        sent.append((message, level, channel))

    monkeypatch.setattr(shared.notify, "notify", up)
    assert _cycle(checks, T0 + timedelta(minutes=1)) == [
        "Health alert: agent_turns is unhealthy. a"
    ]


def test_one_broken_component_does_not_block_the_rest(sent):
    async def broken():
        raise RuntimeError("boom")

    checks = {
        "agent_turns": broken,
        "factory_stuck": _check([{"ok": False, "detail": "stuck"}]),
    }
    assert _cycle(checks, T0) == ["Health alert: factory_stuck is unhealthy. stuck"]


def test_loop_is_dark_unless_enabled(monkeypatch):
    monkeypatch.delenv(health_alerts.ENABLED_ENV, raising=False)
    assert health_alerts.start_health_alert_loop() == []


def test_backup_failure_unknown_handover_reminder_and_recovery(sent, monkeypatch):
    from factory import ops_health

    targets = [{"namespace": "monolith", "cluster": "monolith-pg"}]
    row = dict(
        targets[0],
        status="observed",
        export_ok=True,
        observed_at=T0.isoformat(),
        age_seconds=60,
        has_successful_backup=False,
        last_successful_backup=None,
    )
    document = {"version": 1, "targets": [row]}
    state = [ops_health.evaluate_cnpg_backups(document, targets, [], T0)]
    check = ops_health.CHECKS["cnpg_backups"]
    monkeypatch.setattr(check, "compute", _check(state))
    monkeypatch.setattr(ops_health, "CHECKS", {"cnpg_backups": check})
    check.reset()
    assert len(_cycle(None, T0)) == 1
    # A new process reads the durable announcement, not an in-memory latch.
    check.reset()
    assert _cycle(None, T0 + timedelta(minutes=1)) == []
    state[0] = ops_health.evaluate_cnpg_backups(None, targets, [], T0)
    check.reset()
    assert _cycle(None, T0 + timedelta(hours=7)) == []
    with Session(sent.engine) as s:
        latch = s.get(PlatformProbe, "health_alert.cnpg_backups")
        assert latch.ok is False
        assert latch.checked_at.replace(tzinfo=timezone.utc) == T0
    # Failed export of a healthy backup also cannot clear the alert.
    row.update(
        has_successful_backup=True,
        last_successful_backup=(T0 - timedelta(seconds=60)).isoformat(),
        export_ok=False,
    )
    state[0] = ops_health.evaluate_cnpg_backups(document, targets, [], T0)
    check.reset()
    assert _cycle(None, T0 + timedelta(hours=7, minutes=1)) == []
    row.update(has_successful_backup=False, last_successful_backup=None, export_ok=True)
    state[0] = ops_health.evaluate_cnpg_backups(document, targets, [], T0)
    check.reset()
    assert "still unhealthy" in _cycle(None, T0 + timedelta(hours=7, minutes=2))[0]
    # Recovery must enqueue successfully before the stored state changes.
    row.update(
        has_successful_backup=True,
        last_successful_backup=(T0 - timedelta(seconds=60)).isoformat(),
    )
    state[0] = ops_health.evaluate_cnpg_backups(document, targets, [], T0)

    async def fail(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    original_notify = shared.notify.notify
    monkeypatch.setattr(shared.notify, "notify", fail)
    check.reset()
    assert _cycle(None, T0 + timedelta(hours=7, minutes=3)) == []
    with Session(sent.engine) as s:
        assert s.get(PlatformProbe, "health_alert.cnpg_backups").ok is False
    monkeypatch.setattr(shared.notify, "notify", original_notify)
    check.reset()
    assert (
        "Health recovered: cnpg_backups"
        in _cycle(None, T0 + timedelta(hours=7, minutes=4))[0]
    )
    check.reset()
    assert _cycle(None, T0 + timedelta(hours=7, minutes=5)) == []
    assert [level for _, level, _ in sent] == ["warn", "warn", "info"]


def test_periodic_delegation_resolves_interval_once(monkeypatch):
    import asyncio
    import sys
    from unittest.mock import Mock
    from factory import health_alerts as owner

    interval = Mock(return_value=23.5)
    failure = RuntimeError("original failure")
    report = Mock()
    delegated = []

    async def periodic(cycle, delay, report_failure):
        delegated.append((cycle, delay))
        assert interval.call_count == 1
        try:
            raise failure
        except RuntimeError:
            report_failure()
            assert sys.exception() is failure
        raise asyncio.CancelledError

    def log_failure(message):
        report(message, sys.exception())

    monkeypatch.setattr(owner, "_float_env", interval)
    monkeypatch.setattr(owner, "run_periodic", periodic)
    monkeypatch.setattr(owner.logger, "exception", log_failure)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(owner._loop())
    assert delegated == [(owner.run_cycle, 23.5)]
    interval.assert_called_once_with(owner.INTERVAL_ENV, owner.DEFAULT_INTERVAL_S)
    report.assert_called_once_with("health alert cycle failed", failure)


def test_periodic_task_keeps_name_supervision_and_ownership(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import Mock
    from factory import health_alerts as owner
    import framework

    async def scenario():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def periodic(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        done = Mock()
        monkeypatch.setattr(owner, "run_periodic", periodic)
        monkeypatch.setattr(framework, "log_task_exception", done)
        monkeypatch.setenv(owner.ENABLED_ENV, "true")
        tasks = owner.start_health_alert_loop()
        assert len(tasks) == 1
        task = tasks[0]
        try:
            assert task.get_name() == "factory-health-alerts"

            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert stopped.is_set()
            done.assert_called_once_with(task)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
