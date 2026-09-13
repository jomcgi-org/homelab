"""When an uncertain factory attempt's guest stop becomes due."""

from datetime import datetime, timedelta, timezone

import pytest

from swarm import factory_supervision as supervisor


DISPATCHED_AT = datetime(2026, 9, 11, 8, 13, 34, tzinfo=timezone.utc)


def _case(*, turn_timeout_seconds, failed_after=None, task_after=14400):
    snapshot = {
        "deadline_at": (DISPATCHED_AT + timedelta(seconds=task_after)).isoformat()
    }
    identity = {"dispatched_at": DISPATCHED_AT.isoformat()}
    if failed_after is not None:
        identity["failed_turn_at"] = (
            DISPATCHED_AT + timedelta(seconds=failed_after)
        ).isoformat()
    pin = {"turn_timeout_seconds": turn_timeout_seconds}
    return supervisor._stop_deadline(snapshot, identity, pin)


def test_a_dead_turn_is_due_a_fixed_grace_after_the_failure():
    """The live defect: a policy-maximum timeout cost one task four hours.

    investigate_cache was dispatched with turn_timeout_seconds at the policy
    value of 14400 and its turn died four minutes in. Supervision was refused
    factory_stop_not_due until the timeout elapsed. The failure stamp now
    decides it instead, and since supervision only ever sees a terminal turn,
    this is the deadline that governs every real attempt.
    """
    assert supervisor.STOP_GRACE_SECONDS == 120
    deadline = _case(turn_timeout_seconds=14400, failed_after=240)
    assert deadline == DISPATCHED_AT + timedelta(seconds=360)


def test_a_missing_failure_stamp_falls_back_to_the_turn_timeout():
    """Supervision never reaches this, and the arithmetic stays total anyway.

    read_uncertain_factory_attempt (agent_sessions/reconciliation.py) refuses
    an attempt with factory_attempt_not_uncertain unless its turn is already
    terminal with terminal_reason "error", and it returns failed_turn_at
    unconditionally for the attempts it does admit. A live turn is therefore
    out of scope upstream rather than handled here. This pins the floor the
    guard leaves behind, so a caller that ever built an identity by hand gets
    the old deadline rather than a KeyError.
    """
    deadline = _case(turn_timeout_seconds=14400)
    assert deadline == DISPATCHED_AT + timedelta(seconds=14400)


def test_a_short_turn_timeout_still_wins_when_it_is_the_earlier_one():
    """Every term is a minimum, so the grace never delays a stop."""
    deadline = _case(turn_timeout_seconds=60, failed_after=240)
    assert deadline == DISPATCHED_AT + timedelta(seconds=60)


def test_the_task_deadline_still_bounds_a_dead_turn():
    deadline = _case(turn_timeout_seconds=14400, failed_after=240, task_after=90)
    assert deadline == DISPATCHED_AT + timedelta(seconds=90)


def test_a_pinned_task_deadline_bounds_the_grace_too():
    snapshot = {"deadline_at": (DISPATCHED_AT + timedelta(hours=4)).isoformat()}
    identity = {
        "dispatched_at": DISPATCHED_AT.isoformat(),
        "failed_turn_at": (DISPATCHED_AT + timedelta(seconds=240)).isoformat(),
    }
    pin = {
        "turn_timeout_seconds": 14400,
        "task_deadline_at": (DISPATCHED_AT + timedelta(seconds=30)).isoformat(),
    }
    assert supervisor._stop_deadline(snapshot, identity, pin) == DISPATCHED_AT + (
        timedelta(seconds=30)
    )


def test_a_naive_failure_stamp_is_read_as_utc():
    """Turn timestamps come back from SQLite with no timezone attached."""
    snapshot = {"deadline_at": (DISPATCHED_AT + timedelta(hours=4)).isoformat()}
    naive = (DISPATCHED_AT + timedelta(seconds=240)).replace(tzinfo=None)
    identity = {
        "dispatched_at": DISPATCHED_AT.isoformat(),
        "failed_turn_at": naive.isoformat(),
    }
    pin = {"turn_timeout_seconds": 14400}
    assert supervisor._stop_deadline(
        snapshot, identity, pin
    ) == DISPATCHED_AT + timedelta(seconds=360)


def _departed_node_case(monkeypatch, inventory, *, age=700, state="parked"):
    from cluster import kubernetes

    now = datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc)
    notes = []
    destroys = []

    async def node_names():
        return inventory

    monkeypatch.setattr(supervisor, "_now", lambda: now)
    monkeypatch.setattr(kubernetes, "cluster_node_names", node_names)
    monkeypatch.setattr(
        supervisor,
        "_destroy_guest",
        lambda guest, precondition: destroys.append((guest, precondition)),
    )
    monkeypatch.setattr(
        supervisor,
        "_node_gone_note",
        lambda pin, reason, node_id, **detail: notes.append(
            (pin, reason, node_id, detail)
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "_reserve_node_gone_destroy",
        lambda pin, node_id, precondition: (
            notes.append(
                (
                    pin,
                    "guest_node_gone_destroy_requested",
                    node_id,
                    {"precondition": precondition},
                )
            )
            or True
        ),
    )
    handled = supervisor._destroy_guest_on_departed_node(
        {"task_id": "task-1", "workflow_id": "workflow-1"},
        {"guest_id": "guest-1"},
        {
            "state": state,
            "generation": 7,
            "node": {"node_id": "node-gone"},
            "updated_at": int((now - timedelta(seconds=age)).timestamp() * 1000),
        },
    )
    return handled, destroys, notes


def test_old_parked_guest_on_absent_node_requests_destroy_and_audits(monkeypatch):
    handled, destroys, notes = _departed_node_case(monkeypatch, {"node-present"})

    assert handled
    assert destroys == [("guest-1", {"generation": 7})]
    assert notes == [
        (
            {"task_id": "task-1", "workflow_id": "workflow-1"},
            "guest_node_gone_destroy_requested",
            "node-gone",
            {"precondition": {"generation": 7}},
        )
    ]


@pytest.mark.parametrize("inventory", [{"node-gone"}, None])
def test_present_or_unknown_node_inventory_does_not_destroy(monkeypatch, inventory):
    handled, destroys, notes = _departed_node_case(monkeypatch, inventory)

    assert not handled
    assert destroys == []
    assert notes == []


def test_node_gone_grace_prevents_a_young_guest_destroy(monkeypatch):
    handled, destroys, notes = _departed_node_case(
        monkeypatch, {"node-present"}, age=599
    )

    assert not handled
    assert destroys == []
    assert notes == []


def test_only_parked_or_banked_guests_use_node_gone_destroy(monkeypatch):
    handled, destroys, notes = _departed_node_case(
        monkeypatch, {"node-present"}, state="running"
    )

    assert not handled
    assert destroys == []
    assert notes == []


def test_node_gone_destroy_failure_audits_the_exception_class(monkeypatch):
    _handled, _destroys, notes = _departed_node_case(monkeypatch, {"node-present"})

    def fail(_guest, _precondition):
        raise TimeoutError("control plane timeout")

    monkeypatch.setattr(supervisor, "_destroy_guest", fail)
    now = datetime(2026, 9, 13, 3, 0, tzinfo=timezone.utc)
    handled = supervisor._destroy_guest_on_departed_node(
        {"task_id": "task-1", "workflow_id": "workflow-1"},
        {"guest_id": "guest-1"},
        {
            "state": "parked",
            "generation": 7,
            "node": {"node_id": "node-gone"},
            "updated_at": int((now - timedelta(seconds=700)).timestamp() * 1000),
        },
    )

    assert handled
    assert notes[-1][1:] == (
        "guest_node_gone_destroy_failed",
        "node-gone",
        {"exception": "TimeoutError", "intervention_required": True},
    )


def test_destroyed_view_after_node_gone_uses_existing_cessation_path():
    failed_at = datetime(2026, 9, 13, 2, 30, tzinfo=timezone.utc)
    started = int((failed_at - timedelta(minutes=5)).timestamp() * 1000)
    updated = int((failed_at + timedelta(minutes=5)).timestamp() * 1000)
    identity = {
        "guest_id": "guest-1",
        "dispatched_at": (failed_at - timedelta(minutes=6)).isoformat(),
        "failed_turn_at": failed_at.isoformat(),
    }

    proof = supervisor._control_plane_cessation(
        {
            "session_id": "guest-1",
            "state": "destroyed",
            "generation": 0,
            "invoke_started_at": started,
            "last_invoke_at": started + 1,
            "updated_at": updated,
        },
        identity,
    )

    assert proof is not None
    assert proof["state"] == "destroyed"
