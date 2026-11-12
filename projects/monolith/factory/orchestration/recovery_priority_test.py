"""Persisted recovery/admission arbitration, including PostgreSQL contention."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import (
    factory_controls as controls,
    factory_landing as landing,
)
from factory.orchestration import work_items
from factory.orchestration.factory_intake import admit_next, receive_issue
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask
from factory.orchestration.github_read_cooldown import GitHubReadCooldown

HEAD = "a" * 40
MOVED = "b" * 40


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path, monkeypatch):
    schema = None
    if request.param == "postgres":
        schema = "priority_" + uuid4().hex
        engine = create_engine(
            request.getfixturevalue("pg").url,
            execution_options={"schema_translate_map": {"swarm": schema}},
        )
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'priority.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
            execution_options={"schema_translate_map": {"swarm": None}},
        )

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                SwarmNodeRun,
                FactoryClassTier,
                FactoryControl,
                FactoryReceipt,
                FactoryReviewVerdict,
                FactoryStart,
                FactoryAudit,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
            )
        ],
    )
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    yield engine
    if schema is not None:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
    engine.dispose()


@pytest.fixture
def policy(db, monkeypatch):
    policy = controls.validate_policy(
        {
            "repo": "test/priority-" + uuid4().hex,
            "issue_numbers": list(range(1, 20)),
            "generation": 0,
            "max_tasks": {"delivery": 1, "advisory": 1},
            "max_turns_per_task": 3,
            "task_budget_usd": 5.0,
            "turn_budget_usd": 2.0,
            "allowed_models": ["opus", "luna"],
            "conductor_model": "opus",
            "worker_model": "luna",
            "base_branch": "main",
            "turn_timeout_seconds": 60,
            "max_attempts": 2,
            "task_timeout_seconds": 3600,
            "auto_merge": True,
        }
    )
    with Session(db) as session, session.begin():
        control = session.get(FactoryControl, "factory")
        if control is None:
            control = FactoryControl(id="factory", actor="test")
            session.add(control)
        control.state = "enabled"
        control.policy_json = json.dumps(policy)

    def github(repo, suffix):
        assert repo == policy["repo"]
        if suffix.startswith("issues/"):
            return {"state": "open", "labels": []}
        return {
            "state": "open",
            "merged": False,
            "draft": False,
            "head": {"sha": HEAD},
            "node_id": suffix,
            "auto_merge": None,
        }

    monkeypatch.setattr(landing, "github_get", github)
    monkeypatch.setattr(landing, "_queued_ids", lambda _ids: set())
    return policy


def receipt(db, policy, number, state="landing", **fields):
    task_id = "priority-" + uuid4().hex
    with Session(db) as session, session.begin():
        session.add(
            SwarmTask(
                id=task_id,
                task_text="priority regression",
                conductor_model="opus",
                repo=policy["repo"],
                base_branch="main",
                budget_usd=5,
                start_state="factory",
            )
        )
        session.flush()
        session.add(
            FactoryReceipt(
                repo=policy["repo"],
                issue_number=number,
                title="priority regression",
                body="test",
                url=f"https://github.com/{policy['repo']}/issues/{number}",
                actor="test",
                task_id=task_id,
                state=state,
                policy_json=json.dumps(policy),
                allowance_json='{"turns":3,"spent_usd":2}',
                **fields,
            )
        )
    return task_id


def state(db, task_id, new=None):
    with Session(db) as session, session.begin():
        row = controls._receipt(session, task_id)
        if new is not None:
            row.state = new
        return row.state


def queue(db, policy, number=3, task_class="bug-fix"):
    result = receive_issue(
        policy["repo"],
        number,
        "competing admission",
        "test",
        f"https://github.com/{policy['repo']}/issues/{number}",
        "test",
        task_class=task_class,
    )
    return {"receipt_id": result["receipt"]["id"]}


def request_recovery(task_id, reason="merge_conflict"):
    return controls.request_landing_recovery(
        task_id,
        7711,
        HEAD,
        "delivered_pr",
        "test",
        reason=reason,
        current_head_sha=MOVED if reason == "head_moved" else None,
    )


def audits(db, task_id, action):
    with Session(db) as session:
        return session.exec(
            select(FactoryAudit)
            .where(FactoryAudit.task_id == task_id, FactoryAudit.action == action)
            .order_by(FactoryAudit.id)
        ).all()


@pytest.mark.parametrize("reason", controls.LANDING_RECOVERY_REASONS)
@pytest.mark.parametrize("new_intake", [False, True])
@pytest.mark.parametrize("landing_tick", [False, True])
def test_recovery_wins_freed_slot_and_admission_resumes(
    db, policy, monkeypatch, reason, new_intake, landing_tick
):
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    if not new_intake:
        queued = queue(db, policy)
    assert request_recovery(recovery, reason) == {
        "ok": False,
        "reason": "delivery_capacity",
        "pending": True,
    }
    [request] = audits(db, recovery, "landing_recovery_requested")
    original = request.detail_json
    assert json.loads(original)["deadline_at"] is None
    state(db, holder, "succeeded")
    if new_intake:
        queued = queue(db, policy)
    original_get = landing.github_get
    if reason == "head_moved":

        def moved(repo, suffix):
            result = original_get(repo, suffix)
            if suffix.startswith("pulls/"):
                result["head"] = {"sha": MOVED}
            return result

        monkeypatch.setattr(landing, "github_get", moved)

    if landing_tick:
        # No historical delivery/arming work is needed to retry a durable
        # request. Exercise the production tick with the persisted capacity.
        landing.landing_tick(policy)
        assert state(db, recovery) == "admitted"
    assert not admit_next("test", lanes=("delivery",))["ok"]
    assert state(db, recovery) == "admitted"
    with Session(db) as session:
        assert session.get(FactoryReceipt, queued["receipt_id"]).state == "queued"
        row = controls._receipt(session, recovery)
        assert row.allowance_json == '{"turns":3,"spent_usd":2}'
        assert session.get(SwarmTask, recovery).start_state == "factory"
        occupying = controls.execution_slot_holders(
            policy,
            session.exec(select(FactoryReceipt)).all(),
            unresolved_task_ids=set(),
        )
        assert [r.task_id for r in occupying] == [recovery]
    [started] = audits(db, recovery, "landing_recovery_started")
    detail = json.loads(started.detail_json)
    assert detail["request_id"] == request.id
    assert detail["deadline_at"] is not None
    for _ in range(3):
        assert not admit_next("test", lanes=("delivery",))["ok"]
    assert [
        r.detail_json for r in audits(db, recovery, "landing_recovery_requested")
    ] == [original]
    assert [
        r.detail_json for r in audits(db, recovery, "landing_recovery_started")
    ] == [started.detail_json]
    state(db, recovery, "succeeded")
    result = admit_next("test", lanes=("delivery",))
    assert result["ok"]
    assert result["receipt_id"] == queued["receipt_id"]


@pytest.mark.parametrize(
    "guard",
    [
        "hold",
        "paused",
        "cancelled",
        "uncertain",
        "failed",
        "unresolved",
        "duplicate",
        "operator",
        "review_only",
        "human",
        "skipped",
        "closed_pr",
        "draft",
        "moved",
        "queued_pr",
        "unreadable",
    ],
)
def test_ineligible_older_request_does_not_starve_eligible_recovery(
    db, policy, monkeypatch, guard
):
    holder = receipt(db, policy, 1, "admitted")
    older = receipt(db, policy, 2)
    newer = receipt(db, policy, 4)
    queue(db, policy)
    assert request_recovery(older)["pending"]
    assert request_recovery(newer)["pending"]
    with Session(db) as session, session.begin():
        row = controls._receipt(session, older)
        if guard == "hold":
            row.direction_json = '{"landing_hold":null}'
        elif guard == "paused":
            row.task_paused = True
        elif guard == "cancelled":
            row.cancellation_requested = True
        elif guard in ("uncertain", "failed"):
            row.state = guard
            # Uncertain retains real capacity, so it must be paused only after
            # its unresolved work is settled before any other start can fit.
            if guard == "uncertain":
                row.task_paused = True
        elif guard == "unresolved":
            session.add(
                FactoryStart(
                    task_id=older,
                    start_key="implement_1:1",
                    actor="test",
                    model="opus",
                    max_cost_usd=2,
                    status="reserved",
                )
            )
        elif guard in ("review_only", "human", "skipped"):
            session.add(
                FactoryAudit(
                    task_id=older,
                    actor="test",
                    action={
                        "review_only": "review_recovery_authorized",
                        "human": "human_handoff",
                        "skipped": "landing_recovery_skipped",
                    }[guard],
                    detail_json="{}",
                )
            )
    if guard == "operator":
        monkeypatch.setattr(
            controls, "operator_landing_target", lambda task_id: task_id == older
        )
    if guard == "duplicate":
        receipt(db, policy, 2, "admitted", task_paused=True, generation=1)
    original_get = landing.github_get
    calls = 0

    def observation(repo, suffix):
        nonlocal calls
        result = original_get(repo, suffix)
        # Requests share a PR here; only the older candidate's first read differs.
        if calls == 0 and suffix.startswith("issues/"):
            calls += 1
            if guard == "unreadable":
                raise ValueError("unavailable ownership")
        elif calls == 1 and suffix.startswith("pulls/"):
            calls += 1
            if guard == "closed_pr":
                result["state"] = "closed"
            if guard == "draft":
                result["draft"] = True
            if guard == "moved":
                result["head"] = {"sha": MOVED}
            if guard == "queued_pr":
                result["auto_merge"] = {}
        return result

    monkeypatch.setattr(landing, "github_get", observation)
    state(db, holder, "succeeded")
    assert not admit_next("test", lanes=("delivery",))["ok"]
    assert state(db, newer) == "admitted"
    assert not audits(db, older, "landing_recovery_started")


def test_multiple_requests_start_in_order_and_advisory_is_independent(db, policy):
    holder = receipt(db, policy, 1, "admitted")
    first, second = receipt(db, policy, 2), receipt(db, policy, 4)
    assert request_recovery(first)["pending"]
    assert request_recovery(second)["pending"]
    queued = queue(db, policy)
    advisory = queue(db, policy, 5, "advisory-diagnosis")
    assert (
        admit_next("test", lanes=("advisory",))["receipt_id"] == advisory["receipt_id"]
    )
    state(db, holder, "succeeded")
    assert not admit_next("test", lanes=("delivery",))["ok"]
    assert state(db, first) == "admitted"
    assert state(db, second) == "landing"
    state(db, first, "succeeded")
    assert not admit_next("test", lanes=("delivery",))["ok"]
    assert state(db, second) == "admitted"
    state(db, second, "succeeded")
    assert admit_next("test", lanes=("delivery",))["receipt_id"] == queued["receipt_id"]


@pytest.mark.parametrize("read", ["issue", "pull", "queue"])
@pytest.mark.parametrize("lanes", [("delivery",), ("delivery", "advisory")])
def test_cooldown_does_not_block_delivery_or_advisory_admission(
    db, policy, monkeypatch, read, lanes
):
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery)["pending"]
    [request] = audits(db, recovery, "landing_recovery_requested")
    queued = queue(db, policy)
    advisory = queue(db, policy, 5, "advisory-diagnosis")
    state(db, holder, "succeeded")
    now = 0
    cooldown = GitHubReadCooldown(monotonic=lambda: now, wall_time=lambda: 0)
    assert cooldown.observe(
        {"status": 429, "classification": "rate_limited", "retry_after": "3600"}
    )
    original_get = landing.github_get
    deferred = []

    def check():
        deferred.append(read)
        cooldown.check()
        return set()

    def github(repo, suffix):
        if (read == "issue" and suffix.startswith("issues/")) or (
            read == "pull" and suffix.startswith("pulls/")
        ):
            check()
        return original_get(repo, suffix)

    monkeypatch.setattr(landing, "github_get", github)
    if read == "queue":
        monkeypatch.setattr(landing, "_queued_ids", lambda _ids: check())
    admitted = admit_next("test", lanes=lanes)
    assert admitted["ok"], admitted
    assert admitted["receipt_id"] == queued["receipt_id"]
    assert deferred == [read]
    assert (
        admit_next("test", lanes=("advisory",))["receipt_id"] == advisory["receipt_id"]
    )
    assert deferred == [read]
    assert state(db, recovery) == "landing"
    assert not audits(db, recovery, "landing_recovery_started")
    assert not audits(db, recovery, "landing_recovery_skipped")
    assert [
        row.detail_json for row in audits(db, recovery, "landing_recovery_requested")
    ] == [request.detail_json]
    now = 3602
    state(db, admitted["task_id"], "succeeded")
    queue(db, policy, 6)
    assert not admit_next("test", lanes=("delivery",))["ok"]
    assert state(db, recovery) == "admitted"
    [started] = audits(db, recovery, "landing_recovery_started")
    assert json.loads(started.detail_json)["request_id"] == request.id


@pytest.mark.parametrize(
    "action", ["landing_recovery_skipped", "merged", "human_handoff"]
)
def test_only_round_local_skip_expires_at_new_request(db, policy, action):
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery)["pending"]
    [old_request] = audits(db, recovery, "landing_recovery_requested")
    with Session(db) as session, session.begin():
        controls._audit(session, "test", action, task_id=recovery)
        session.flush()
        controls._audit(
            session,
            "test",
            "landing_recovery_requested",
            task_id=recovery,
            **json.loads(old_request.detail_json),
        )
    queued = queue(db, policy)
    state(db, holder, "succeeded")
    result = admit_next("test", lanes=("delivery",))
    if action == "landing_recovery_skipped":
        assert not result["ok"]
        assert state(db, recovery) == "admitted"
        [started] = audits(db, recovery, "landing_recovery_started")
        latest = audits(db, recovery, "landing_recovery_requested")[-1]
        assert json.loads(started.detail_json)["request_id"] == latest.id
    else:
        assert result["receipt_id"] == queued["receipt_id"]
        assert state(db, recovery) == "landing"
        assert not audits(db, recovery, "landing_recovery_started")


@pytest.mark.parametrize("mismatch", ["pr_number", "head_sha"])
@pytest.mark.parametrize("pending", [False, True])
def test_replayed_recovery_rejects_different_pr_or_head(db, policy, mismatch, pending):
    if pending:
        holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    original = request_recovery(recovery)
    assert original.get("pending") if pending else original["ok"]
    [request] = audits(db, recovery, "landing_recovery_requested")
    with Session(db) as session:
        allowance = controls._receipt(session, recovery).allowance_json
        deadline = controls._landing_recovery_deadline(session, recovery)
    if pending:
        state(db, holder, "succeeded")
    result = controls.request_landing_recovery(
        recovery,
        7712 if mismatch == "pr_number" else 7711,
        MOVED if mismatch == "head_sha" else HEAD,
        "merge_queue",
        "test",
    )
    assert result == {"ok": False, "reason": "recovery_authority_mismatch"}
    assert state(db, recovery) == ("landing" if pending else "admitted")
    assert [
        row.detail_json for row in audits(db, recovery, "landing_recovery_requested")
    ] == [request.detail_json]
    assert not audits(db, recovery, "landing_recovery_started")
    with Session(db) as session:
        assert controls._receipt(session, recovery).allowance_json == allowance
        assert controls._landing_recovery_deadline(session, recovery) == deadline
    replay = request_recovery(recovery)
    assert replay["ok"] and replay["replayed"]


@pytest.mark.parametrize(
    "guard", ["closed", "draft", "moved", "missing_head", "missing_state"]
)
def test_unready_pr_reads_are_bounded_without_fencing_unavailable_evidence(
    db, policy, monkeypatch, guard
):
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery)["pending"]
    [request] = audits(db, recovery, "landing_recovery_requested")
    with controls._locked_session() as (_session, control):
        updated = json.loads(control.policy_json)
        updated["auto_merge"] = False
        control.policy_json = json.dumps(updated)
    state(db, holder, "succeeded")
    original_get = landing.github_get
    reads = []

    def github(repo, suffix):
        reads.append(suffix)
        result = original_get(repo, suffix)
        if suffix.startswith("pulls/"):
            if guard == "closed":
                result["state"] = "closed"
            elif guard == "draft":
                result["draft"] = True
            elif guard == "moved":
                result["head"]["sha"] = MOVED
            elif guard == "missing_head":
                result.pop("head")
            else:
                result.pop("state")
        return result

    monkeypatch.setattr(landing, "github_get", github)
    for _ in range(3):
        assert not admit_next("test", lanes=("delivery",))["ok"]
    skipped = audits(db, recovery, "landing_recovery_skipped")
    if guard.startswith("missing_"):
        assert len(reads) == 6
        assert not skipped
    else:
        assert len(reads) == 2
        assert len(skipped) == 1
        assert json.loads(skipped[0].detail_json)["request_id"] == request.id
    assert state(db, recovery) == "landing"
    assert not audits(db, recovery, "landing_recovery_started")


@pytest.mark.parametrize(
    "change",
    ["disabled", "paused", "stopped", "repo_paused", "repo_disabled", "auto_merge"],
)
def test_policy_guards_do_not_reopen_recovery(db, policy, monkeypatch, change):
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery, "review_stale")["pending"]
    queued = queue(db, policy)
    state(db, holder, "succeeded")
    with controls._locked_session() as (_session, control):
        if change in ("disabled", "paused", "stopped"):
            control.state = change
        else:
            updated = json.loads(control.policy_json)
            if change == "auto_merge":
                updated["auto_merge"] = False
            else:
                updated["repos"] = {
                    policy["repo"]: {
                        "paused" if change == "repo_paused" else "enabled": change
                        == "repo_paused"
                    }
                }
            control.policy_json = json.dumps(updated)

    def unexpected_read(*_args):
        pytest.fail("policy-excluded recovery must not read GitHub")

    monkeypatch.setattr(landing, "github_get", unexpected_read)
    result = admit_next("test", lanes=("delivery",))
    assert state(db, recovery) == "landing"
    assert not audits(db, recovery, "landing_recovery_started")
    if change == "auto_merge":
        assert result["receipt_id"] == queued["receipt_id"]
        # Passing a new reason on retry cannot downgrade recorded authority.
        assert request_recovery(recovery)["reason"] == "factory_disabled"


def test_existing_deadline_and_limit_are_never_renewed(db, policy, monkeypatch):
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery)["ok"]
    state(db, recovery, "landing")
    [original] = audits(db, recovery, "landing_recovery_requested")
    future = controls._now() + timedelta(
        seconds=controls.LANDING_RECOVERY_TIMEOUT_SECONDS + 1
    )
    monkeypatch.setattr(controls, "_now", lambda: future)
    assert request_recovery(recovery)["reason"] == "landing_recovery_deadline"
    assert (
        audits(db, recovery, "landing_recovery_requested")[0].detail_json
        == original.detail_json
    )
    assert not audits(db, recovery, "landing_recovery_started")
    other = receipt(db, policy, 4)
    monkeypatch.setattr(controls, "MAX_LANDING_RECOVERIES", 0)
    assert request_recovery(other)["reason"] == "recovery_limit"
    assert not audits(db, other, "landing_recovery_requested")


@pytest.mark.parametrize("reason", controls.LANDING_RECOVERY_REASONS)
@pytest.mark.parametrize("caller", ["queued", "new_intake", "recovery"])
def test_postgres_competing_callers_share_control_lock(
    db, policy, monkeypatch, reason, caller
):
    if db.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock guarantee is tested on PostgreSQL only")
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery, reason)["pending"]
    queued = queue(db, policy)
    state(db, holder, "succeeded")
    observing, release, competing = Event(), Event(), Event()
    original_get = landing.github_get

    def blocked_observation(repo, suffix):
        observing.set()
        assert release.wait(10)
        result = original_get(repo, suffix)
        if reason == "head_moved" and suffix.startswith("pulls/"):
            result["head"] = {"sha": MOVED}
        return result

    monkeypatch.setattr(landing, "github_get", blocked_observation)

    def contender():
        competing.set()
        if caller == "new_intake":
            queue(db, policy, 6)
        elif caller == "recovery":
            return request_recovery(recovery, reason)
        return admit_next("competing", lanes=("delivery",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(admit_next, "first", lanes=("delivery",))
        assert observing.wait(5)
        loser = pool.submit(contender)
        assert competing.wait(5)
        try:
            # PostgreSQL's lock wait is authoritative; a SQLite scheduling
            # outcome or merely observing that a thread has started is not.
            from sqlalchemy import text

            deadline = monotonic() + 5
            blocked = 0
            while monotonic() < deadline:
                with db.connect() as connection:
                    blocked = connection.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%factory_control%'"
                        )
                    ).scalar_one()
                if blocked:
                    break
                release.wait(0.01)
            assert blocked >= 1
            assert not loser.done()
        finally:
            release.set()
        assert not winner.result(timeout=10)["ok"]
        assert loser.result(timeout=10)["ok"] == (caller == "recovery")
    assert state(db, recovery) == "admitted"
    assert len(audits(db, recovery, "landing_recovery_started")) == 1
    with Session(db) as session:
        assert session.get(FactoryReceipt, queued["receipt_id"]).state == "queued"
        occupying = controls.execution_slot_holders(
            policy,
            session.exec(select(FactoryReceipt)).all(),
            unresolved_task_ids=set(),
        )
        assert [row.task_id for row in occupying] == [recovery]


@pytest.mark.parametrize(
    "fence",
    [
        "held",
        "human_issue",
        "round_recorded",
        "superseded",
        "queue_member",
        "unreadable",
    ],
)
def test_only_ineligible_pending_work_does_not_reserve_admission_capacity(
    db, policy, monkeypatch, fence
):
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery)["pending"]
    [request] = audits(db, recovery, "landing_recovery_requested")
    queued = queue(db, policy)
    if fence in ("held", "round_recorded", "superseded"):
        with Session(db) as session, session.begin():
            if fence == "held":
                controls._receipt(
                    session, recovery
                ).direction_json = '{"landing_hold":null}'
            elif fence == "round_recorded":
                session.add(
                    FactoryAudit(
                        actor="test",
                        task_id=recovery,
                        action="landing_recovery_round",
                        detail_json=json.dumps({"request_id": request.id}),
                    )
                )
            else:
                # The latest request supersedes this old null-deadline row.
                detail = json.loads(request.detail_json)
                detail["deadline_at"] = (
                    controls._now() + timedelta(minutes=30)
                ).isoformat()
                session.add(
                    FactoryAudit(
                        actor="test",
                        task_id=recovery,
                        action="landing_recovery_requested",
                        detail_json=json.dumps(detail),
                    )
                )
    if fence == "queue_member":
        monkeypatch.setattr(landing, "_queued_ids", lambda ids: set(ids))
    original_get = landing.github_get

    def observation(repo, suffix):
        if fence == "unreadable":
            raise ValueError("unknown GitHub evidence")
        result = original_get(repo, suffix)
        if fence == "human_issue" and suffix.startswith("issues/"):
            result["labels"] = [{"name": "human"}]
        return result

    monkeypatch.setattr(landing, "github_get", observation)
    state(db, holder, "succeeded")
    assert admit_next("test", lanes=("delivery",))["receipt_id"] == queued["receipt_id"]
    assert state(db, recovery) == "landing"
    assert not audits(db, recovery, "landing_recovery_started")


def test_recovery_and_admission_share_transaction_rollback(db, policy, monkeypatch):
    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery)["pending"]
    queue(db, policy)
    state(db, holder, "succeeded")
    original = controls.request_landing_recovery

    def interrupted(*args, **kwargs):
        assert original(*args, **kwargs)["ok"]
        raise RuntimeError("interrupted transaction")

    monkeypatch.setattr(controls, "request_landing_recovery", interrupted)
    with pytest.raises(RuntimeError, match="interrupted transaction"):
        admit_next("test", lanes=("delivery",))
    assert state(db, recovery) == "landing"
    assert not audits(db, recovery, "landing_recovery_started")
    monkeypatch.setattr(controls, "request_landing_recovery", original)
    assert not admit_next("test", lanes=("delivery",))["ok"]
    assert state(db, recovery) == "admitted"


@pytest.mark.parametrize(
    "guard",
    [
        "parent_missing",
        "parent_paused",
        "parent_cancelled",
        "parent_held",
        "owner_author",
        "owner_hold",
        "owner_unreadable",
        "owner_unheld",
    ],
)
def test_parent_and_owner_pr_holds_remain_binding(db, policy, monkeypatch, guard):
    from factory.orchestration import owner_prs

    holder = receipt(db, policy, 1, "admitted")
    recovery = receipt(db, policy, 2)
    assert request_recovery(recovery)["pending"]
    queued = queue(db, policy)
    direction = {}
    if guard.startswith("parent_"):
        parent = "missing"
        if guard != "parent_missing":
            parent = receipt(db, policy, 4, "succeeded")
            with Session(db) as session, session.begin():
                row = controls._receipt(session, parent)
                if guard == "parent_paused":
                    row.task_paused = True
                elif guard == "parent_cancelled":
                    row.cancellation_requested = True
                else:
                    row.direction_json = '{"landing_hold":null}'
        direction["rollback_parent_task_id"] = parent
    else:
        direction["owner_pr"] = {"author_id": 42}
        original_get = landing.github_get

        def observation(repo, suffix):
            result = original_get(repo, suffix)
            if suffix.startswith("pulls/"):
                result["user"] = {"id": 43 if guard == "owner_author" else 42}
                if guard == "owner_hold":
                    result["labels"] = [{"name": "do-not-merge"}]
            return result

        def comments(repo, suffix):
            if guard == "owner_unreadable":
                raise ValueError("owner PR discussion unavailable")
            return []

        monkeypatch.setattr(landing, "github_get", observation)
        monkeypatch.setattr(landing, "github_list", comments)
        monkeypatch.setattr(owner_prs.hold_release, "read_release", lambda *args: None)
    with Session(db) as session, session.begin():
        controls._receipt(session, recovery).direction_json = json.dumps(direction)
    state(db, holder, "succeeded")
    result = admit_next("test", lanes=("delivery",))
    if guard == "owner_unheld":
        assert not result["ok"]
        assert state(db, recovery) == "admitted"
        assert len(audits(db, recovery, "landing_recovery_started")) == 1
    else:
        assert result["receipt_id"] == queued["receipt_id"]
        assert state(db, recovery) == "landing"
        assert not audits(db, recovery, "landing_recovery_started")
