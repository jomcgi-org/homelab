"""When an uncertain factory attempt's guest stop becomes due."""

from datetime import datetime, timedelta, timezone

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
    decides it instead.
    """
    assert supervisor.STOP_GRACE_SECONDS == 120
    deadline = _case(turn_timeout_seconds=14400, failed_after=240)
    assert deadline == DISPATCHED_AT + timedelta(seconds=360)


def test_an_open_turn_keeps_its_turn_timeout_deadline():
    """No failure stamp means the turn could still be running."""
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
