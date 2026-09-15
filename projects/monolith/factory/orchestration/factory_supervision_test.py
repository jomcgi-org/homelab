"""When an uncertain factory attempt's guest stop becomes due."""

from datetime import datetime, timedelta, timezone

import pytest

from factory.orchestration import factory_supervision as supervisor


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


ABSENT_AT = datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
ABSENT_IDENTITY = {
    "guest_id": "guest-9",
    "identity_sha256": "sha-9",
    "cost_usd": None,
}


class _Run:
    def __init__(self, *, cost_usd=None, outcome_json=None):
        self.cost_usd = cost_usd
        self.outcome_json = outcome_json
        self.session_id = 5381
        self.base_sha = "base-sha"
        self.head_sha = "head-sha"


def _absence_harness(
    monkeypatch,
    *,
    records,
    now=ABSENT_AT,
    identity=None,
    run=None,
    attempt_identity=None,
):
    """Drive _absence_settled with the locks and the audit trail stubbed out."""
    import contextlib

    identity = identity or dict(ABSENT_IDENTITY)
    run = run or _Run()
    audits = []
    settlements = []

    @contextlib.contextmanager
    def locked(*_args, **_kwargs):
        yield ("db", "control")

    monkeypatch.setattr(supervisor, "_now", lambda: now)
    monkeypatch.setattr(supervisor.controls, "_locked_session", locked)
    monkeypatch.setattr(
        supervisor,
        "_locked_attempt",
        lambda db, control, pin, sid, **kw: (
            attempt_identity if attempt_identity is not None else identity,
            run,
        ),
    )
    monkeypatch.setattr(supervisor, "_records", lambda db, pin: records)
    monkeypatch.setattr(
        supervisor,
        "_audit",
        lambda db, pin, action, **detail: audits.append((action, detail)),
    )
    monkeypatch.setattr(
        supervisor,
        "_settle_failed_attempt",
        lambda *a, **kw: settlements.append(kw) or {"status": "failed"},
    )
    settled = supervisor._absence_settled(
        {"task_id": "task-1", "workflow_id": "workflow-1"},
        5381,
        identity,
        {"status": "uncertain"},
    )
    return settled, audits, settlements


def _absence_record(seconds_before, *, sha="sha-9"):
    return (
        "stop_absence",
        {
            "identity_sha256": sha,
            "recorded_at": (ABSENT_AT - timedelta(seconds=seconds_before)).isoformat(),
        },
    )


def test_a_single_absence_records_an_observation_and_settles_nothing(monkeypatch):
    """One 404 is not cessation: a control plane can lose sight of a live guest."""
    settled, audits, settlements = _absence_harness(monkeypatch, records=[])

    assert settled is False
    assert settlements == []
    assert audits == [
        (
            "stop_absence",
            {
                "identity_sha256": "sha-9",
                "observation": 1,
                "guest_id": "guest-9",
                "cessation_confirmed": False,
                "intervention_required": False,
            },
        )
    ]


def test_absence_below_the_observation_floor_does_not_settle(monkeypatch):
    """Two prior observations still leave the count short of the floor."""
    assert supervisor.MIN_ABSENCE_OBSERVATIONS == 3
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[_absence_record(3600), _absence_record(1800)],
    )

    assert settled is False
    assert settlements == []
    assert audits[0][1]["observation"] == 3


def test_enough_observations_that_have_not_held_long_enough_do_not_settle(monkeypatch):
    """The count is met but the span is not, so the reservation stays held."""
    assert supervisor.ABSENCE_CONFIRM_SECONDS == 900
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(899),
            _absence_record(600),
            _absence_record(300),
        ],
    )

    assert settled is False
    assert settlements == []
    # Nothing further is written once the floor is reached: the audit stream
    # stays bounded however long the control plane keeps answering 404.
    assert audits == []


def test_sustained_absence_settles_the_attempt_failed(monkeypatch):
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(1800),
            _absence_record(1200),
            _absence_record(600),
        ],
    )

    assert settled is True
    assert len(settlements) == 1
    assert settlements[0]["evidence_key"] == "absence"
    assert settlements[0]["reason"].startswith("guest_absent_confirmed:")
    assert settlements[0]["evidence"]["observations"] == 3
    assert settlements[0]["evidence"]["held_seconds"] == 1800
    assert settlements[0]["evidence"]["guest_id"] == "guest-9"
    assert audits[-1][0] == "stop_settled"
    assert audits[-1][1]["cessation_confirmed"] is True
    assert audits[-1][1]["intervention_required"] is False


def test_absence_observations_from_another_attempt_do_not_count(monkeypatch):
    """Evidence is pinned to identity_sha256, so a new attempt starts again."""
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(1800, sha="sha-other"),
            _absence_record(1200, sha="sha-other"),
            _absence_record(600, sha="sha-other"),
        ],
    )

    assert settled is False
    assert settlements == []
    assert audits[0][1]["observation"] == 1


def test_an_already_settled_attempt_is_not_settled_twice(monkeypatch):
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[("stop_settled", {"recorded_at": ABSENT_AT.isoformat()})],
    )

    assert settled is True
    assert settlements == []
    assert audits == []


def test_an_attempt_that_changed_underneath_supervision_is_refused(monkeypatch):
    with pytest.raises(ValueError, match="factory_attempt_changed"):
        _absence_harness(
            monkeypatch,
            records=[],
            attempt_identity={"guest_id": "guest-other", "identity_sha256": "sha-x"},
        )


class _Ok:
    ok = True
    refusal_code = None


def _settle_harness(monkeypatch, *, run, original_result, identity=None):
    """Drive _settle_failed_attempt with the graph and start writes stubbed."""
    identity = identity or dict(ABSENT_IDENTITY)
    outcomes = []
    charges = []

    monkeypatch.setattr(
        supervisor, "settle_uncertain_factory_attempt", lambda db, pin, ident: None
    )
    monkeypatch.setattr(
        supervisor.graph, "record_dispatch", lambda *a, **kw: _Ok(), raising=False
    )
    monkeypatch.setattr(
        supervisor.graph,
        "record_outcome",
        lambda *a, **kw: outcomes.append(a) or _Ok(),
        raising=False,
    )
    monkeypatch.setattr(
        supervisor.controls,
        "record_start_outcome",
        lambda *a, **kw: charges.append(kw) or {"ok": True},
    )
    result = supervisor._settle_failed_attempt(
        "db",
        {
            "task_id": "task-1",
            "workflow_id": "workflow-1",
            "node_key": "n",
            "attempt": 1,
        },
        5381,
        identity,
        run,
        original_result,
        reason="guest_absent_confirmed: test",
        evidence_key="absence",
        evidence={"guest_id": "guest-9"},
    )
    return result, outcomes, charges


def test_a_settled_absent_guest_keeps_the_largest_known_cost(monkeypatch):
    """A bound guest ran, so its reservation is consumed rather than refunded.

    This is the difference from the never-bound proofs, which settle at a
    measured zero because nothing was ever invoked.
    """
    identity = dict(ABSENT_IDENTITY, cost_usd=2.5)
    result, _outcomes, charges = _settle_harness(
        monkeypatch,
        run=_Run(cost_usd=1.25),
        original_result={"status": "uncertain", "cost_usd": 0.5},
        identity=identity,
    )

    assert result["status"] == "failed"
    assert result["cost_usd"] == 2.5
    assert result["absence"] == {"guest_id": "guest-9"}
    assert charges[0]["cost_usd"] == 2.5
    assert charges[0]["reconciled"] is True


def test_a_settlement_with_no_known_cost_stays_unknown(monkeypatch):
    result, _outcomes, charges = _settle_harness(
        monkeypatch,
        run=_Run(),
        original_result={"status": "uncertain"},
    )

    assert result["cost_usd"] is None
    assert charges[0]["cost_usd"] is None


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), "3"])
def test_a_nonsense_recorded_cost_refuses_the_settlement(monkeypatch, bad):
    """Fail closed: a cost that cannot be compared never releases a slot."""
    with pytest.raises(ValueError, match="invalid_original_cost"):
        _settle_harness(
            monkeypatch,
            run=_Run(cost_usd=bad),
            original_result={"status": "uncertain"},
        )


def _reconcile_harness(monkeypatch, raised):
    """Drive reconcile_uncertain_attempt far enough to reach the guest read."""
    import contextlib

    notes = []
    absences = []

    @contextlib.contextmanager
    def locked(*_args, **_kwargs):
        yield ("db", "control")

    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "true")
    monkeypatch.setattr(supervisor.controls, "_locked_session", locked)
    monkeypatch.setattr(supervisor, "_records", lambda db, pin: [])
    monkeypatch.setattr(
        supervisor,
        "_locked_attempt",
        lambda db, control, pin, sid, **kw: (dict(ABSENT_IDENTITY), _Run()),
    )
    monkeypatch.setattr(supervisor, "_note", lambda pin, reason: notes.append(reason))

    def read(_guest, precondition=None):
        raise raised

    monkeypatch.setattr(supervisor, "_http", read)
    monkeypatch.setattr(
        supervisor,
        "_absence_settled",
        lambda pin, sid, ident, original: absences.append(ident) or True,
    )
    settled = supervisor.reconcile_uncertain_attempt(
        {"task_id": "task-1", "workflow_id": "workflow-1"},
        5381,
        {"status": "uncertain"},
        "ERROR",
    )
    return settled, notes, absences


def test_an_authoritative_404_routes_to_the_absence_proof(monkeypatch):
    from factory.execution.transport import EmberSessionGone

    settled, notes, absences = _reconcile_harness(
        monkeypatch, EmberSessionGone("404 not found")
    )

    assert settled is True
    assert len(absences) == 1
    assert notes == []


def test_an_unreachable_control_plane_is_never_read_as_absence(monkeypatch):
    """A 500 or a timeout must not release a guest that may still be running."""
    settled, notes, absences = _reconcile_harness(
        monkeypatch, TimeoutError("control plane timeout")
    )

    assert settled is False
    assert absences == []
    assert notes == ["stop_observation_unavailable"]


# --- deadline backstop (factory_conductor) ----------------------------------

BACKSTOP_NOW = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)


class _Start:
    def __init__(self, status, *, start_key="s-1", session_id=5381):
        self.status = status
        self.start_key = start_key
        self.session_id = session_id


def _task(*, expired=True, past_seconds=7200):
    from factory.orchestration import factory_conductor

    return {
        "task_id": "t-1",
        "repo": "jomcgi-org/homelab",
        "issue_number": 4040,
        "limits": {"deadline_expired": expired},
        "deadline_at": (
            BACKSTOP_NOW
            - timedelta(
                seconds=past_seconds
                or factory_conductor.FACTORY_DEADLINE_BACKSTOP_GRACE_SECONDS
            )
        ).isoformat(),
    }


def _backstop_harness(monkeypatch, *, starts, task=None, enabled=True, finish_ok=True):
    import contextlib

    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_controls as controls

    finishes = []
    outcomes = []
    escalations = []
    notifies = []

    @contextlib.contextmanager
    def locked(*_args, **_kwargs):
        yield ("db", "control")

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setenv(
        "FACTORY_DEADLINE_BACKSTOP_ENABLED", "true" if enabled else "false"
    )
    monkeypatch.setattr(conductor, "Session", lambda _engine: _Db())
    monkeypatch.setattr(conductor, "get_engine", lambda: None)
    monkeypatch.setattr(controls, "_locked_session", locked)
    monkeypatch.setattr(controls, "_starts", lambda db, task_id: starts)
    monkeypatch.setattr(
        controls,
        "record_start_outcome",
        lambda *a, **kw: outcomes.append(kw) or {"ok": True},
    )
    monkeypatch.setattr(
        controls,
        "finish_task",
        lambda task_id, outcome, actor, **kw: (
            finishes.append((outcome, kw)) or {"ok": finish_ok}
        ),
    )
    monkeypatch.setattr(controls, "_audit", lambda *a, **kw: None)
    monkeypatch.setattr(
        conductor, "_record_escalation", lambda tid, doc: escalations.append(doc)
    )
    monkeypatch.setattr(conductor, "_post_decision_card", lambda *a, **kw: "card-url")
    monkeypatch.setattr(conductor, "_decision_card", lambda doc: "card")
    monkeypatch.setattr(conductor, "_escalation_marker", lambda tid: "marker")
    monkeypatch.setattr(
        conductor, "_notify_escalation", lambda *a, **kw: notifies.append(a)
    )
    monkeypatch.setattr(conductor, "_warn_deadline_tripped", lambda task: None)
    monkeypatch.setattr(
        "factory.orchestration.factory_landing.github_write", lambda *a, **kw: {}
    )
    released = conductor._expire_task_deadline(task or _task())
    return released, finishes, outcomes, escalations, notifies


def test_the_backstop_releases_a_stranded_start_and_escalates(monkeypatch):
    released, finishes, outcomes, escalations, notifies = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain"), _Start("failed", start_key="s-0")]
    )

    assert released is True
    assert [kw["cost_usd"] for kw in outcomes] == [0.0]
    assert outcomes[0]["reconciled"] is True
    assert finishes[0][0] == "escalated"
    assert finishes[0][1]["evidence"]["state"] == "deadline_backstop_expired"
    assert escalations[0]["options"][0]["effect"] == "agent-ready"
    assert len(notifies) == 1


def test_the_backstop_never_interrupts_a_reserved_start(monkeypatch):
    """A reserved start is live work. The backstop only collects the dead."""
    released, finishes, outcomes, escalations, _n = _backstop_harness(
        monkeypatch, starts=[_Start("reserved"), _Start("uncertain")]
    )

    assert released is False
    assert outcomes == []
    assert finishes == []
    assert escalations == []


def test_the_backstop_does_nothing_with_no_uncertain_start(monkeypatch):
    released, finishes, outcomes, _e, _n = _backstop_harness(
        monkeypatch, starts=[_Start("succeeded"), _Start("failed")]
    )

    assert released is False
    assert outcomes == []
    assert finishes == []


def test_the_backstop_waits_out_the_grace_after_the_deadline(monkeypatch):
    """Inside the grace the proofs still own it, so nothing is released."""
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "_deadline_backstop_due", lambda task: False)
    released, finishes, outcomes, _e, _n = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")]
    )

    assert released is False
    assert outcomes == []
    assert finishes == []


def test_the_backstop_is_off_by_default(monkeypatch):
    released, finishes, outcomes, _e, _n = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], enabled=False
    )

    assert released is False
    assert outcomes == []
    assert finishes == []


def test_a_refused_finish_leaves_the_task_for_the_next_tick(monkeypatch):
    released, finishes, _o, _e, notifies = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], finish_ok=False
    )

    assert released is False
    assert finishes[0][0] == "escalated"
    assert notifies == []


@pytest.mark.parametrize(
    "past,due", [(7200, True), (7201, True), (7199, False), (0, False)]
)
def test_the_backstop_grace_is_measured_from_the_deadline(monkeypatch, past, due):
    from factory.orchestration import factory_conductor as conductor

    assert conductor.FACTORY_DEADLINE_BACKSTOP_GRACE_SECONDS == 7200
    deadline = BACKSTOP_NOW - timedelta(seconds=past)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return BACKSTOP_NOW

    monkeypatch.setattr(conductor, "datetime", _Clock)
    assert (
        conductor._deadline_backstop_due({"deadline_at": deadline.isoformat()}) is due
    )


@pytest.mark.parametrize("stamp", [None, 17, "not-a-timestamp"])
def test_an_unusable_deadline_never_triggers_the_backstop(stamp):
    """Fail closed: a deadline that cannot be read releases nothing."""
    from factory.orchestration import factory_conductor as conductor

    assert conductor._deadline_backstop_due({"deadline_at": stamp}) is False
