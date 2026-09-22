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


def _backstop_harness(
    monkeypatch,
    *,
    starts,
    task=None,
    enabled=True,
    staged=True,
    finish_ok=True,
    permit_seq=1,
    permit_seqs=None,
    permit_missing=False,
    permit_error=False,
):
    import contextlib
    from types import SimpleNamespace

    from sqlalchemy.exc import OperationalError

    from factory.execution import admission, reconciliation
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_controls as controls

    finishes = []
    outcomes = []
    escalations = []
    notifies = []
    settled_permits = []

    @contextlib.contextmanager
    def locked(*_args, **_kwargs):
        yield ("db", "control")

    commits = []

    class _Savepoint:
        def commit(self):
            pass

        def rollback(self):
            pass

    live_seqs = [permit_seq] if permit_seqs is None else list(permit_seqs)

    class _Result:
        def first(self):
            if permit_missing or not live_seqs:
                return None
            return SimpleNamespace(pending_seq=live_seqs[0])

        def all(self):
            if permit_missing:
                return []
            return [SimpleNamespace(pending_seq=seq) for seq in live_seqs]

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def commit(self):
            commits.append(True)

        def begin_nested(self):
            return _Savepoint()

        def exec(self, _statement):
            return _Result()

    def settle_permit(_db, _agent, seq, *, outcome, cessation_confirmed):
        if permit_error:
            raise OperationalError("settle", {}, Exception("permit unavailable"))
        settled_permits.append({"seq": seq, "outcome": outcome})

    monkeypatch.setattr(admission, "settle", settle_permit)
    monkeypatch.setattr(
        reconciliation,
        "_locked_session",
        lambda _db, session_id: {"session_id": session_id},
    )

    monkeypatch.setenv(
        "FACTORY_DEADLINE_BACKSTOP_ENABLED", "true" if enabled else "false"
    )
    monkeypatch.setattr(conductor, "FACTORY_DEADLINE_BACKSTOP_RELEASE_STAGED", staged)
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
    monkeypatch.setattr(conductor.graph, "node_runs", lambda *a, **kw: [])
    monkeypatch.setattr(
        "factory.orchestration.factory_landing.github_write", lambda *a, **kw: {}
    )
    released = conductor._expire_task_deadline(task or _task())
    return (
        released,
        finishes,
        outcomes,
        escalations,
        notifies,
        commits,
        settled_permits,
    )


def test_the_backstop_releases_a_stranded_start_and_escalates(monkeypatch):
    released, finishes, outcomes, escalations, notifies, commits, _p = (
        _backstop_harness(
            monkeypatch, starts=[_Start("uncertain"), _Start("failed", start_key="s-0")]
        )
    )

    assert released is True
    assert outcomes[0]["reconciled"] is True
    assert finishes[0][0] == "escalated"
    assert finishes[0][1]["evidence"]["state"] == "deadline_backstop_expired"
    # Carrying on must stay first: resume_escalated applies options[0] without
    # showing the card, so leading with hold would make Resume a terminal
    # no-op that consumes the escalation. The guest warning lives in the text.
    assert escalations[0]["options"][0]["effect"] == "agent-ready"
    assert escalations[0]["recommendation"] == "deliver"
    assert [option["effect"] for option in escalations[0]["options"]] == [
        "agent-ready",
        "hold",
    ]
    assert "confirm it is gone" in escalations[0]["question"]
    assert [kw["cost_usd"] for kw in outcomes] == [None]
    assert len(notifies) == 1
    # The settlement is only real if it committed.
    assert commits == [True]


def test_the_backstop_never_interrupts_a_reserved_start(monkeypatch):
    """A reserved start is live work. The backstop only collects the dead."""
    released, finishes, outcomes, escalations, _n, commits, _p = _backstop_harness(
        monkeypatch, starts=[_Start("reserved"), _Start("uncertain")]
    )

    assert released is False
    assert outcomes == []
    assert finishes == []
    assert escalations == []


def test_the_backstop_does_nothing_with_no_uncertain_start(monkeypatch):
    released, finishes, outcomes, _e, _n, commits, _p = _backstop_harness(
        monkeypatch, starts=[_Start("succeeded"), _Start("failed")]
    )

    assert released is False
    assert outcomes == []
    assert finishes == []


def test_the_backstop_waits_out_the_grace_after_the_deadline(monkeypatch):
    """Inside the grace the proofs still own it, so nothing is released."""
    from factory.orchestration import factory_conductor as conductor

    monkeypatch.setattr(conductor, "_deadline_backstop_due", lambda task: False)
    released, finishes, outcomes, _e, _n, commits, _p = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")]
    )

    assert released is False
    assert outcomes == []
    assert finishes == []


def test_the_backstop_is_off_by_default(monkeypatch):
    released, finishes, outcomes, _e, _n, commits, _p = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], enabled=False
    )

    assert released is False
    assert outcomes == []
    assert finishes == []


def test_the_backstop_cannot_release_while_repository_delivery_is_staged(
    monkeypatch,
):
    released, finishes, outcomes, escalations, _n, commits, permits = _backstop_harness(
        monkeypatch,
        starts=[_Start("uncertain")],
        enabled=True,
        staged=False,
    )

    assert released is False
    assert outcomes == []
    assert finishes == []
    assert escalations == []
    assert commits == []
    assert permits == []


def test_a_refused_finish_leaves_the_task_for_the_next_tick(monkeypatch):
    released, finishes, _o, _e, notifies, commits, _p = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], finish_ok=False
    )

    assert released is False
    assert finishes[0][0] == "escalated"
    assert notifies == []
    # A refused finish must discard the settlement rather than commit it.
    assert commits == []


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


def test_the_backstop_refuses_an_operator_paused_task(monkeypatch):
    """A pause is a deliberate hold, and settling through it is irreversible.

    Once the receipt leaves _ACTIVE, resume_task answers task_not_active, so
    the operator could not undo their own pause. _expire_reconciler_pause
    refuses any pause it did not set; the backstop never sets one, so it
    refuses all of them.
    """
    released, finishes, outcomes, escalations, _n, commits, _p = _backstop_harness(
        monkeypatch,
        starts=[_Start("uncertain")],
        task=dict(_task(), task_paused=True),
    )

    assert released is False
    assert outcomes == []
    assert finishes == []
    assert escalations == []
    assert commits == []


def test_the_escalation_names_the_permits_it_cannot_release(monkeypatch):
    """The backstop frees the lane slot but cannot free the capacity permit.

    admission.settle only frees a reservation on confirmed cessation, which is
    exactly what the backstop lacks, so the permit stays uncertain and keeps
    counting against the pool. Unnamed, that reads as a healthy lane that
    cannot start anything.
    """
    _released, _f, _o, escalations, _n, _c, _p = _backstop_harness(
        monkeypatch,
        starts=[_Start("uncertain", session_id=5381)],
    )

    assert "5381" in escalations[0]["reason"]
    assert "separate operator step" in escalations[0]["reason"]


def test_the_backstop_settles_the_permit_at_its_own_sequence(monkeypatch):
    """The lane slot and the admission permit have to be released together.

    Freeing the start alone leaves the reservation counted by reserve_start,
    so intake admits fresh work that can never get a guest: a churning lane
    rather than a visibly stuck one.
    """
    released, _f, _o, _e, _n, _c, permits = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], permit_seq=2
    )

    assert released is True
    # Never seq 1 by assumption: a second reservation on one session is
    # ordinary, and hardcoding it settles the wrong permit.
    assert permits == [{"seq": 2, "outcome": "deadline_backstop_released"}]


def test_the_backstop_does_not_claim_cessation_it_never_proved(monkeypatch):
    """The permit outcome must not read as confirmed cessation.

    The backstop releases on a deadline without proving the guest stopped,
    and its own escalation card says so.
    """
    _r, _f, _o, _e, _n, _c, permits = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")]
    )

    assert [p["outcome"] for p in permits] == ["deadline_backstop_released"]


def test_the_backstop_finishes_when_the_permit_cannot_be_settled(monkeypatch):
    """A permit failure must not abort the start settlement or the finish."""
    released, finishes, _o, _e, _n, commits, permits = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], permit_error=True
    )

    assert released is True
    assert permits == []
    assert finishes and commits


def test_the_backstop_skips_a_session_with_no_unsettled_permit(monkeypatch):
    """A start whose permit is already settled still releases the lane slot."""
    released, finishes, _o, _e, _n, commits, permits = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], permit_missing=True
    )

    assert released is True
    assert permits == []
    assert finishes and commits


def test_the_backstop_releases_every_live_permit_on_the_session(monkeypatch):
    """One session can hold more than one unsettled permit.

    The unique constraint is on (session_id, pending_seq), and the ambiguity
    guard that holds supervision to exactly one permit does not run on this
    path. Releasing only the first row found would leave the rest counted
    against background_limit and leak the capacity this exists to reclaim.
    """
    released, _f, _o, _e, _n, _c, permits = _backstop_harness(
        monkeypatch, starts=[_Start("uncertain")], permit_seqs=[1, 2]
    )

    assert released is True
    assert [entry["seq"] for entry in permits] == [1, 2]
    assert {entry["outcome"] for entry in permits} == {"deadline_backstop_released"}
