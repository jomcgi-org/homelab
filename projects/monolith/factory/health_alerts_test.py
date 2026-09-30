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
