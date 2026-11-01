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


@pytest.mark.parametrize("state", ["running", "banked", "destroyed"])
def test_destroyed_view_after_node_gone_uses_existing_cessation_path(state):
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
            "state": state,
            "interrupted_turn": {"seq": 1},
            "generation": 0,
            "invoke_started_at": started,
            "last_invoke_at": started + 1,
            "updated_at": updated,
        },
        identity,
    )

    if state == "destroyed":
        assert proof is not None
        assert proof["state"] == "destroyed"
    else:
        assert proof is None


def _restart_precondition(*, generation=4, invoke_started_at=100):
    return {
        "session_id": "guest-1",
        "generation": generation,
        "invoke_started_at": invoke_started_at,
        "vm_id": "vm-1",
        "node_id": "node-1",
        "instance_id": "node-1/pod-1",
        "pod_uid": "pod-1",
        "boot_id": "boot-1",
    }


def test_post_restart_generation_change_is_cessation_evidence():
    recorded = _restart_precondition()
    current = {
        **recorded,
        "generation": recorded["generation"] + 1,
        "invoke_started_at": None,
        "vm_id": "vm-2",
        "instance_id": "node-1/pod-2",
        "pod_uid": "pod-2",
        "boot_id": "boot-2",
    }

    proof = supervisor._replacement_invocation_cessation(
        {
            "session_id": "guest-1",
            "state": "running",
            "generation": current["generation"],
            "invoke_started_at": current["invoke_started_at"],
            "last_invoke_at": None,
            "stop_precondition": current,
        },
        {"guest_id": "guest-1"},
        {"precondition": recorded},
    )

    assert proof["replacement_evidence"] == "generation_advanced"
    assert proof["previous_generation"] == 4
    assert proof["generation"] == 5


def test_post_restart_invoke_stamp_reset_is_cessation_evidence():
    recorded = _restart_precondition()
    current = {**recorded, "invoke_started_at": None}

    proof = supervisor._replacement_invocation_cessation(
        {
            "session_id": "guest-1",
            "state": "running",
            "generation": current["generation"],
            "invoke_started_at": None,
            "last_invoke_at": recorded["invoke_started_at"],
            "stop_precondition": current,
        },
        {"guest_id": "guest-1"},
        {"precondition": recorded},
    )

    assert proof["replacement_evidence"] == "invoke_completed"
    assert proof["previous_invoke_started_at"] == 100
    assert proof["invoke_started_at"] is None


def test_unordered_invoke_stamp_reset_is_not_cessation_evidence():
    recorded = _restart_precondition()
    current = {**recorded, "invoke_started_at": None}

    assert (
        supervisor._replacement_invocation_cessation(
            {
                "session_id": "guest-1",
                "state": "running",
                "generation": current["generation"],
                "invoke_started_at": None,
                "last_invoke_at": None,
                "stop_precondition": current,
            },
            {"guest_id": "guest-1"},
            {"precondition": recorded},
        )
        is None
    )


def test_supervision_note_records_the_value_error_refusal(monkeypatch):
    import contextlib

    audits = []

    @contextlib.contextmanager
    def locked():
        yield ("db", "control")

    monkeypatch.setattr(supervisor.controls, "_locked_session", locked)
    monkeypatch.setattr(supervisor, "_records", lambda db, pin: [])
    monkeypatch.setattr(
        supervisor,
        "_audit",
        lambda db, pin, action, **detail: audits.append((action, detail)),
    )

    supervisor._note(
        {"task_id": "task-1", "workflow_id": "workflow-1"},
        "stop_evidence_or_ownership_changed",
        error="changed_stop_invocation",
    )

    assert audits == [
        (
            "stop_observation",
            {
                "reason": "stop_evidence_or_ownership_changed",
                "error": "changed_stop_invocation",
                "intervention_required": True,
                "cessation_confirmed": False,
            },
        )
    ]


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


def _stamp(seconds_before):
    return (ABSENT_AT - timedelta(seconds=seconds_before)).isoformat()


def _absence_record(seconds_before, *, sha="sha-9"):
    return (
        "stop_absence",
        {"identity_sha256": sha, "recorded_at": _stamp(seconds_before)},
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


def test_absence_is_sampled_on_an_interval_not_on_every_tick(monkeypatch):
    """A fresh observation is only written once the interval has elapsed.

    Recording every tick would take the whole count in one burst and then
    settle on a span it never actually watched.
    """
    assert supervisor.ABSENCE_OBSERVATION_INTERVAL_SECONDS == 300
    settled, audits, settlements = _absence_harness(
        monkeypatch, records=[_absence_record(299)]
    )

    assert settled is False
    assert settlements == []
    assert audits == []


def test_absence_below_the_observation_floor_does_not_settle(monkeypatch):
    assert supervisor.MIN_ABSENCE_OBSERVATIONS == 3
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[_absence_record(600), _absence_record(300)],
    )

    assert settled is False
    assert settlements == []
    assert audits[0][1]["observation"] == 3


def test_enough_observations_that_have_not_held_long_enough_do_not_settle(monkeypatch):
    """The count is met but the watched span is not, so the slot stays held."""
    assert supervisor.ABSENCE_CONFIRM_SECONDS == 900
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(500),
            _absence_record(300),
            _absence_record(100),
        ],
    )

    assert settled is False
    assert settlements == []


def test_sustained_absence_settles_the_attempt_failed(monkeypatch):
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(910),
            _absence_record(610),
            _absence_record(310),
            _absence_record(10),
        ],
    )

    assert settled is True
    assert len(settlements) == 1
    assert settlements[0]["evidence_key"] == "absence"
    assert settlements[0]["reason"].startswith("guest_absent_confirmed:")
    assert settlements[0]["evidence"]["observations"] == 4
    assert settlements[0]["evidence"]["held_seconds"] == 900
    assert settlements[0]["evidence"]["guest_id"] == "guest-9"
    assert audits[-1][0] == "stop_settled"
    assert audits[-1][1]["cessation_confirmed"] is True
    assert audits[-1][1]["intervention_required"] is False


def test_an_intermittent_404_cannot_add_up_to_a_release(monkeypatch):
    """The live defect this proof has to survive.

    A guest that is alive, and a control plane that 404s it briefly during two
    separate rollouts hours apart. Counting every absence ever recorded would
    see three readings spanning three hours and release a running guest onto a
    second writer. Only an unbroken run counts, so the old reading is dropped
    and the evidence starts again.
    """
    assert supervisor.ABSENCE_MAX_GAP_SECONDS == 600
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(10800),
            _absence_record(310),
            _absence_record(10),
        ],
    )

    assert settled is False
    assert settlements == []


def test_a_live_observation_between_absences_breaks_the_run(monkeypatch):
    """Any non-absence stop record means a later tick saw something else."""
    settled, _audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(1500),
            _absence_record(1200),
            ("stop_observation", {"recorded_at": _stamp(900), "reason": "x"}),
            _absence_record(610),
            _absence_record(310),
        ],
    )

    assert settled is False
    assert settlements == []


def test_a_full_audit_trail_still_settles(monkeypatch):
    """At the cap no further row is written, and a fresh good span settles."""
    assert supervisor.MAX_ABSENCE_OBSERVATIONS == 8
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[_absence_record(2200 - 300 * n) for n in range(8)],
    )

    assert settled is True
    assert len(settlements) == 1
    assert not [entry for entry in audits if entry[0] == "stop_absence"]


def test_absence_observations_from_another_attempt_do_not_count(monkeypatch):
    """Evidence is pinned to identity_sha256, so a new attempt starts again."""
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(910, sha="sha-other"),
            _absence_record(610, sha="sha-other"),
            _absence_record(310, sha="sha-other"),
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


def test_a_long_run_keeps_sampling_rather_than_freezing(monkeypatch):
    """Suppressing the write past the cap froze the newest reading, and the
    freshness guard then refused that run forever."""
    settled, audits, settlements = _absence_harness(
        monkeypatch,
        records=[_absence_record(7200 + 300 * (7 - n)) for n in range(8)],
    )

    assert settled is False
    assert settlements == []
    # The observation is still written, so the newest reading keeps moving and
    # the freshness guard can be satisfied again.
    assert "stop_absence" in [action for action, _ in audits]


def test_a_stale_run_does_not_settle(monkeypatch):
    """The freshest reading has to be recent, or the evidence stopped moving.

    Below the cap this is implied: settlement is only reached on a tick where
    no observation was due. At the cap the write is skipped, so without the
    guard a run whose newest reading is hours old would settle on evidence
    that had stopped being refreshed, over a window the guest may have spent
    answering.
    """
    settled, _audits, settlements = _absence_harness(
        monkeypatch,
        records=[_absence_record(7200 + 300 * (7 - n)) for n in range(8)],
    )

    assert settled is False
    assert settlements == []


def test_an_overlong_run_is_noted_once(monkeypatch):
    """Past the expected length the run is an anomaly, flagged but still sampled."""
    assert supervisor.MAX_ABSENCE_OBSERVATIONS == 8
    notes = []
    monkeypatch.setattr(supervisor, "_note", lambda pin, reason: notes.append(reason))
    settled, _audits, _s = _absence_harness(
        monkeypatch,
        records=[_absence_record(7200 + 300 * (7 - n)) for n in range(8)],
    )

    assert settled is False
    assert notes == ["absence_run_unsettled"]


def _presence_harness(monkeypatch, *, records):
    """Drive _record_presence with the lock and audit stubbed out."""
    import contextlib

    audits = []

    @contextlib.contextmanager
    def locked(*_args, **_kwargs):
        yield ("db", "control")

    monkeypatch.setattr(supervisor.controls, "_locked_session", locked)
    monkeypatch.setattr(supervisor, "_records", lambda db, pin: records)
    monkeypatch.setattr(
        supervisor,
        "_audit",
        lambda db, pin, action, **detail: audits.append((action, detail)),
    )
    supervisor._record_presence({"task_id": "task-1"}, dict(ABSENT_IDENTITY))
    return audits


def test_an_observed_guest_breaks_an_open_absence_run(monkeypatch):
    """The live defect the gap rule alone does not close.

    Every other record the guest-visible path writes is deduplicated, so after
    the first few ticks a successful 200 read leaves no trace. A control plane
    404ing intermittently but at least once inside every gap window would then
    be indistinguishable from one that had torn the guest down.
    """
    audits = _presence_harness(monkeypatch, records=[_absence_record(10)])

    assert [action for action, _ in audits] == ["stop_presence"]


def test_presence_is_not_recorded_with_no_absence_run_open(monkeypatch):
    """A healthy attempt never accumulates presence rows."""
    audits = _presence_harness(
        monkeypatch,
        records=[("stop_observation", {"recorded_at": _stamp(10), "reason": "x"})],
    )

    assert audits == []


def test_presence_is_never_suppressed_by_a_cap(monkeypatch):
    """The live defect round three found.

    Presence is the only record that can break an absence run once the other
    actions have deduplicated themselves. Capping it meant that after enough
    alternations a guest answering 200 stopped leaving a trace, and a flapping
    control plane could accumulate a release against a running guest again.
    """
    audits = _presence_harness(
        monkeypatch,
        records=[("stop_presence", {"recorded_at": _stamp(99)})] * 20
        + [_absence_record(10)],
    )

    assert [action for action, _ in audits] == ["stop_presence"]


def test_a_presence_record_resets_the_absence_run(monkeypatch):
    """An absence run that straddles a presence row starts again after it."""
    settled, _audits, settlements = _absence_harness(
        monkeypatch,
        records=[
            _absence_record(1800),
            _absence_record(1500),
            ("stop_presence", {"recorded_at": _stamp(1200)}),
            _absence_record(900),
            _absence_record(600),
        ],
    )

    assert settled is False
    assert settlements == []
