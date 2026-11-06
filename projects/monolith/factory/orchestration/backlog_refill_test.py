"""Default-off refill, durable starts, recurrence and publication with fake I/O."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from core.jsonio import dumps
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import backlog_refill as refill
from factory.orchestration import backlog_refill_producer as writer
from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_intake_loop as intake
from factory.orchestration.backlog_refill_policy import DEFAULTS, normalize
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
from factory.orchestration.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
_REAL_REFILL_QUOTA = refill.quota


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'refill.db'}",
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
                SwarmPlanNode,
                SwarmPlanVersion,
                SwarmConductorCall,
                FactoryAudit,
                FactoryControl,
                FactoryReceipt,
                FactoryStart,
                FactoryClassTier,
                FactoryReviewVerdict,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
            )
        ],
    )
    # Existing fixtures exercise the deployed v1 contract and its replay.
    # New source-contract cases explicitly request the inspection fixture.
    if getattr(request, "param", None) != "inspection":
        observe = refill.observe

        def historical_observe(*args, **kwargs):
            result = observe(*args, **kwargs)
            if result.get("ok"):
                with Session(engine) as db:
                    row = refill.latest_event(db, "started", run_id=result["run_id"])
                    detail = refill._detail(row)
                    detail.pop("inspection_contract", None)
                    row.detail_json = dumps(detail)
                    db.commit()
            return result

        monkeypatch.setattr(refill, "observe", historical_observe)
    clock = [NOW]
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: clock[0])
    monkeypatch.setattr(intake, "_now", lambda: clock[0])
    monkeypatch.setattr(
        refill,
        "quota",
        lambda model, *, force=True: {
            "model": model,
            "checked_at": clock[0].isoformat(),
        },
    )
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    policy = controls.validate_policy(
        {
            "repo": "jomcgi-org/homelab",
            "issue_numbers": [1],
            "generation": 0,
            "max_tasks": {"delivery": 2, "advisory": 2},
            "max_turns_per_task": 3,
            "task_budget_usd": 5.0,
            "turn_budget_usd": 2.0,
            "allowed_models": ["opus", "astra"],
            "conductor_model": "opus",
            "worker_model": "opus",
            "base_branch": "main",
            "turn_timeout_seconds": 60,
            "max_attempts": 2,
            "task_timeout_seconds": 3600,
            "intake": {"enabled": True},
            "backlog_refill": {"enabled": True},
        }
    )
    with Session(engine) as db:
        db.add(
            FactoryControl(
                id="factory",
                actor="test",
                state="enabled",
                policy_json=dumps(policy),
            )
        )
        db.commit()
    yield engine, policy, clock
    engine.dispose()


def update(world, **changes):
    engine, policy, _clock = world
    policy.update(changes)
    with Session(engine) as db:
        row = db.get(FactoryControl, "factory")
        row.policy_json = dumps(policy)
        db.commit()


def complete(world, start, *, success=True, new_issues=0, summary="test run"):
    engine, _policy, _clock = world
    with Session(engine) as db:
        db.get(FactoryReceipt, start["receipt_id"]).state = (
            "succeeded" if success else "failed"
        )
        db.commit()
    refill.settle(
        start["run_id"], success=success, new_issues=new_issues, summary=summary
    )


def admit(world, start):
    engine, policy, clock = world
    task_id = "t-" + start["run_id"]
    with Session(engine) as db:
        db.add(
            SwarmTask(
                id=task_id,
                task_text="inventory",
                repo=policy["repo"],
                base_branch=policy["base_branch"],
                conductor_model="opus",
                created_at=clock[0],
                budget_usd=policy["task_budget_usd"],
            )
        )
        db.flush()
        row = db.get(FactoryReceipt, start["receipt_id"])
        row.state, row.task_id, row.policy_json = "admitted", task_id, dumps(policy)
        db.commit()
    return task_id


def publication_start(world):
    """Historical single-role ledger fixture for existing publication semantics."""
    start = refill.observe(world[1], eligible=0, complete=True)
    legacy_start(world, start)
    admit(world, start)
    return start


def legacy_start(world, start):
    import json

    with Session(world[0]) as db:
        row = refill.latest_event(db, "started", run_id=start["run_id"])
        detail = json.loads(row.detail_json)
        detail.pop("review_contract")
        row.detail_json = dumps(detail)
        db.commit()


def test_omitted_legacy_and_recorded_defaults(world):
    _engine, policy, _clock = world
    legacy = {key: value for key, value in policy.items() if key != "backlog_refill"}
    legacy["allowed_models"] = ["opus"]
    assert normalize(legacy) == DEFAULTS
    assert "backlog_refill" not in controls.validate_policy(legacy)
    off = controls.validate_policy({**legacy, "backlog_refill": {"enabled": False}})
    assert normalize(controls.validate_policy(off))["enabled"] is False
    assert controls._policy_for_generation_comparison(
        legacy
    ) == controls._policy_for_generation_comparison({**legacy, "backlog_refill": {}})
    assert normalize(policy)["max_per_24_hours"] == 24
    assert refill.observe(legacy, eligible=0, complete=True)["reason"] == "disabled"
    with controls._read_session() as db:
        assert not refill._events(db, "started")


def test_charter_ownership_and_blocking_follow_the_linked_work_item(world):
    engine, policy, _clock = world
    with Session(engine) as db:
        charter = WorkItem(
            title="Inventory charter",
            state="open",
            source_kind="github",
            trust="trusted",
            authority="github",
            github_repo=policy["repo"],
            github_issue_number=refill.CHARTER,
            labels=["human"],
        )
        blocker = WorkItem(
            title="Blocker",
            state="open",
            source_kind="factory",
            trust="trusted",
        )
        db.add_all([charter, blocker])
        db.commit()
        charter_id, blocker_id = charter.id, blocker.id
    assert (
        refill.observe(policy, eligible=24, complete=True)["reason"]
        == "charter_human_owned"
    )
    with Session(engine) as db:
        db.get(WorkItem, charter_id).labels = []
        db.add(WorkItemEdge(from_id=blocker_id, to_id=charter_id, kind="blocks"))
        db.commit()
    assert (
        refill.observe(policy, eligible=24, complete=True)["reason"]
        == "charter_blocked"
    )
    with Session(engine) as db:
        blocker = db.get(WorkItem, blocker_id)
        blocker.state, blocker.close_reason = "closed", "completed"
        db.commit()
    start = refill.observe(policy, eligible=24, complete=True)
    assert start["ok"]
    with Session(engine) as db:
        assert db.get(FactoryReceipt, start["receipt_id"]).work_item_id == charter_id


def test_omitted_board_does_not_read_refill_ledger(world, monkeypatch):
    _engine, policy, _clock = world
    legacy = {key: value for key, value in policy.items() if key != "backlog_refill"}

    def forbidden(*_args):
        raise AssertionError("omitted refill must not add status SQL reads")

    monkeypatch.setattr(refill, "_ledger", forbidden)
    assert refill.state(legacy)["status"] == "off"


@pytest.mark.parametrize(
    "key,values",
    [
        ("min_eligible", [0, 1001, True, "25"]),
        ("cooldown_minutes", [0, 1441, False, 1.5]),
        ("min_new_issues", [-1, 101, True]),
        ("breaker_consecutive_low_yield", [0, 11, True]),
        ("max_per_24_hours", [0, 25, 48, True]),
        ("enabled", [1, None]),
        ("model", ["sol", None]),
        ("effort", ["unknown", [], None]),
        ("labels", [[], ["bug"], None]),
        ("unknown", [True]),
    ],
)
def test_invalid_policy(world, key, values):
    _engine, policy, _clock = world
    for value in values:
        with pytest.raises(ValueError):
            normalize({**policy, "backlog_refill": {key: value}})


@pytest.mark.parametrize(
    "key,low,high",
    [
        ("min_eligible", 1, 1000),
        ("cooldown_minutes", 1, 1440),
        ("min_new_issues", 0, 100),
        ("breaker_consecutive_low_yield", 1, 10),
        ("max_per_24_hours", 1, 24),
    ],
)
def test_valid_edges(world, key, low, high):
    for value in (low, high):
        assert normalize({**world[1], "backlog_refill": {key: value}})[key] == value


def test_floor_concurrent_ticks_and_restart(world):
    _engine, policy, _clock = world
    assert refill.observe(policy, eligible=25, complete=True)["reason"] == "above_floor"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: refill.observe(policy, eligible=24, complete=True), range(2)
            )
        )
    assert sum(result["ok"] for result in results) == 1
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "in_flight"
    with controls._read_session() as db:
        assert len(refill._events(db, "started")) == 1
    assert refill.state(policy)["status"] == "in flight"


@pytest.mark.parametrize("control_state", ["paused", "stopped", "disabled"])
def test_admission_disabled(world, control_state):
    engine, policy, _clock = world
    with Session(engine) as db:
        db.get(FactoryControl, "factory").state = control_state
        db.commit()
    assert (
        refill.observe(policy, eligible=0, complete=True)["reason"]
        == "admission_disabled"
    )


def test_unknown_coverage_and_capacity(world):
    _engine, policy, _clock = world
    assert (
        refill.observe(policy, eligible=0, complete=False)["reason"]
        == "unknown_coverage"
    )
    assert (
        refill.observe(
            policy, eligible=0, complete=True, observed_at=NOW - timedelta(minutes=5)
        )["reason"]
        == "unknown_coverage"
    )
    assert (
        refill.observe(policy, eligible=0, complete=True, lanes=("delivery",))["reason"]
        == "advisory_capacity"
    )


def test_cooldown_settlement_and_unchanged_reset(world):
    engine, policy, clock = world
    start = refill.observe(policy, eligible=0, complete=True)
    complete(world, start)
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "cooldown"
    clock[0] += timedelta(minutes=15)
    second = refill.observe(policy, eligible=0, complete=True)
    assert second["ok"] and second["run_id"] != start["run_id"]
    with controls._locked_session() as (db, _control):
        refill.reset(db, policy)
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "in_flight"
    with Session(engine) as db:
        assert len(db.exec(select(FactoryReceipt)).all()) == 2


def test_low_yields_once_productive_reset_and_failure_interleaving(world):
    _engine, policy, clock = world
    for success, count in (
        (True, 0),
        (False, 0),
        (True, 3),
        (True, 0),
        (True, 0),
        (False, 0),
    ):
        start = refill.observe(policy, eligible=24, complete=True)
        assert start["ok"]
        complete(
            world, start, success=success, new_issues=count, summary=f"yield={count}"
        )
        complete(world, start, success=success, new_issues=count)
        clock[0] += timedelta(minutes=15)
    assert refill.observe(policy, eligible=24, complete=True)["reason"] == "breaker"
    state = refill.state(policy)
    assert len(state["breaker"]["runs"]) == 3
    assert state["status"] == "breaker open"
    with controls._read_session() as db:
        assert len(refill._events(db, "completed")) == 6
        assert len(refill._events(db, "low_yield")) == 5


@pytest.mark.parametrize(
    "successes", [(True, True, False), (True, False, True), (False, True, False)]
)
@pytest.mark.parametrize("new_issues", [0, 2])
def test_failed_runs_count_toward_low_yield_breaker(world, successes, new_issues):
    _engine, policy, clock = world
    for success in successes:
        start = refill.observe(policy, eligible=0, complete=True)
        assert start["ok"]
        complete(world, start, success=success, new_issues=new_issues)
        clock[0] += timedelta(minutes=61)
    assert refill.state(policy)["breaker"]["reason"] == "low_yield"
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "breaker"


@pytest.mark.parametrize("threshold", [1, 4, 10])
def test_interleaved_low_yield_uses_configured_breaker_count(world, threshold):
    _engine, policy, clock = world
    update(
        world,
        backlog_refill={"enabled": True, "breaker_consecutive_low_yield": threshold},
    )
    for index in range(threshold):
        start = refill.observe(policy, eligible=0, complete=True)
        assert start["ok"]
        complete(world, start, success=index % 3 != 2, new_issues=2)
        state = refill.state(policy)
        if index + 1 < threshold:
            assert state["status"] == "armed"
        clock[0] += timedelta(minutes=61)
    assert state["breaker"]["reason"] == "low_yield"
    assert len(state["breaker"]["runs"]) == threshold
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "breaker"


def test_productive_failure_resets_yield_but_not_failure_streak(world):
    _engine, policy, clock = world
    for success, count in ((True, 0), (True, 0), (False, 3), (True, 0), (False, 3)):
        start = refill.observe(policy, eligible=0, complete=True)
        assert start["ok"]
        complete(world, start, success=success, new_issues=count)
        assert refill.state(policy)["status"] == "armed"
        clock[0] += timedelta(minutes=61)
    start = refill.observe(policy, eligible=0, complete=True)
    assert start["ok"]
    complete(world, start, success=False, new_issues=3)
    assert refill.state(policy)["breaker"]["reason"] == "failed_runs"


def test_two_consecutive_failures_and_explicit_repost(world):
    _engine, policy, clock = world
    for success in (False, True, False, False):
        start = refill.observe(policy, eligible=0, complete=True)
        assert start["ok"]
        complete(world, start, success=success, new_issues=3 if success else 0)
        clock[0] += timedelta(minutes=15)
    assert refill.state(policy)["breaker"]["reason"] == "failed_runs"
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert refill.state(policy)["status"] == "armed"
    assert refill.state(policy)["starts_last_24_hours"] == 4


def test_daily_cap_counts_ambiguous_intents_and_rolling_boundary(world):
    _engine, policy, clock = world
    update(world, backlog_refill={**policy["backlog_refill"], "max_per_24_hours": 1})
    start = refill.observe(policy, eligible=0, complete=True)
    assert refill.state(policy)["starts_last_24_hours"] == 1
    clock[0] += timedelta(hours=1)
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "in_flight"
    complete(world, start, new_issues=3)
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "daily_cap"
    assert refill.state(policy)["breaker"] is None
    assert refill.state(policy)["status"] == "rate limited"
    clock[0] = NOW + timedelta(hours=24)
    assert refill.observe(policy, eligible=0, complete=True)["ok"]


@pytest.fixture
def github(world, monkeypatch):
    issues = []
    calls = []

    def discover(_repo, marker, _block):
        return [item["number"] for item in issues if marker in item["body"]], False

    def create(_repo, payload):
        calls.append(payload)
        result = {**payload, "number": len(issues) + 100}
        issues.append(result)
        return result

    monkeypatch.setattr(writer.producer, "_discover", discover)
    monkeypatch.setattr(writer.producer, "github_write", create)
    return issues, calls


def test_skip_and_collection_reads_are_bounded_and_scoped(world, github):
    from factory.orchestration import backlog_refill_worker as worker

    engine, policy, _clock = world
    start = publication_start(world)
    value = {"summary": "inventory", "findings": []}
    writer.collection(start["run_id"], value)
    with controls._locked_session() as (db, _control):
        detail = refill._detail(refill._events(db, "started")[0])
        refill.record(db, "skipped", repo=policy["repo"], reason="above_floor")
        for number in range(10):
            refill.record(
                db, "skipped", repo=f"other/repo-{number}", reason="in_flight"
            )
            refill.record(
                db,
                "collection",
                run_id=f"other-{number}",
                collection={"summary": "other inventory", "findings": []},
            )

    queries = []

    def bounded(_conn, _cursor, statement, parameters, _context, _many):
        if statement.startswith("SELECT") and any(
            refill.PREFIX + action in parameters for action in ("skipped", "collection")
        ):
            queries.append(statement)
            assert "LIMIT" in statement
            assert "detail_json LIKE" in statement

    event.listen(engine, "before_cursor_execute", bounded)
    try:
        with controls._locked_session() as (db, _control):
            refill._skip(db, policy["repo"], "above_floor")
        writer.collection(start["run_id"], value)
        assert writer.advance(detail)["success"]
        with Session(engine) as db:
            db.get(FactoryReceipt, start["receipt_id"]).state = "succeeded"
            db.commit()
        worker.tick(policy)
        assert refill.state(policy)["in_flight"] == []
    finally:
        event.remove(engine, "before_cursor_execute", bounded)
    assert len(queries) == 5
    with controls._read_session() as db:
        assert len(refill._events(db, "skipped")) == 11
        assert len(refill._events(db, "collection")) == 11


def test_multi_finding_summary_and_breaker_replay(world, github):
    _engine, policy, _clock = world
    start = publication_start(world)
    with controls._read_session() as db:
        detail = refill._detail(refill._events(db, "started")[0])
    # A start from before routines keeps its needs-human summary issue.
    assert detail.pop("routine") == "dead-code"
    value = {
        "summary": "| Project | Evidence |\n| project | source |",
        "findings": [
            {
                "key": f"path:function{i}",
                "title": f"factory: finding {i}",
                "body": "Reproduce with path/command. Acceptance: regression test.",
            }
            for i in range(3)
        ],
    }
    writer.collection(start["run_id"], value)
    writer.collection(start["run_id"], value)
    for _ in range(4):
        outcome = writer.advance(detail)
    assert outcome["new_issues"] == 3
    assert writer.advance(detail) == outcome
    issues, calls = github
    assert len(calls) == 4
    assert calls[-1]["labels"] == ["needs-human"]
    assert all(
        f"<!-- factory-refill:{start['run_id']} -->" in item["body"] for item in issues
    )
    opening = {
        "repo": policy["repo"],
        "runs": [{"run_id": start["run_id"], "summary": "summary"}],
        "opening_id": "one",
        "reason": "low_yield",
    }
    writer.breaker(opening)
    writer.breaker(opening)
    assert len(calls) == 5
    assert calls[-1]["title"] == "factory: backlog refill breaker open"


def test_distinct_new_yield_and_rejected_write(world, github, monkeypatch):
    _engine, policy, _clock = world
    start = publication_start(world)
    item = writer.output(
        policy["repo"],
        start["run_id"],
        "finding",
        "same",
        "factory: existing",
        "body",
        ["agent-ready"],
    )
    assert writer.publish(item)["status"] == "created"
    replay = writer.output(
        policy["repo"],
        "two",
        "finding",
        "renamed-key",
        "factory: existing",
        "body",
        ["agent-ready"],
    )
    assert writer.publish(replay)["status"] == "existing"
    request = httpx.Request("POST", "https://api.github.com/test")

    def rejected(*_args):
        response = httpx.Response(422, request=request)
        raise httpx.HTTPStatusError("rejected", request=request, response=response)

    monkeypatch.setattr(writer.producer, "github_write", rejected)
    other = writer.output(
        policy["repo"],
        start["run_id"],
        "finding",
        "other",
        "factory: other",
        "body",
        ["agent-ready"],
    )
    assert writer.publish(other)["status"] == "rejected"
    assert writer.publish(other)["status"] == "rejected"
    assert len(github[1]) == 1


def test_uncertain_write_survives_disable_and_reconciles(world, github, monkeypatch):
    _engine, policy, clock = world
    create = writer.producer.github_write

    def lost(*args):
        create(*args)
        raise TimeoutError("response lost")

    monkeypatch.setattr(writer.producer, "github_write", lost)
    start = publication_start(world)
    item = writer.output(
        policy["repo"],
        start["run_id"],
        "finding",
        "key",
        "factory: test",
        "body",
        ["agent-ready"],
    )
    assert writer.publish(item) is None
    update(world, backlog_refill={**policy["backlog_refill"], "enabled": False})
    clock[0] += timedelta(minutes=2)
    assert writer.publish(item)["status"] == "created"
    assert len(github[1]) == 1
    assert refill.state(policy)["status"] == "off"


def test_crash_before_post_and_bounded_unknown_reconciliation(
    world, github, monkeypatch
):
    _engine, policy, clock = world

    def crash(*_args):
        raise TimeoutError("no proof of dispatch")

    monkeypatch.setattr(writer.producer, "github_write", crash)
    start = publication_start(world)
    item = writer.output(
        policy["repo"],
        start["run_id"],
        "finding",
        "key",
        "factory: test",
        "body",
        ["agent-ready"],
    )
    writer.publish(item)
    for index in range(20):
        clock[0] += timedelta(hours=1)
        assert writer.publish(item)["status"] == (
            "uncertain" if index < 5 else "quarantined"
        )
    assert not github[1]
    with controls._read_session() as db:
        assert writer._last(db, item["output_id"])["reads"] == 6


def test_backend_to_board_projection(world, monkeypatch):
    from factory import private_view
    from factory.orchestration import factory_landing

    _engine, policy, _clock = world
    monkeypatch.setattr(controls, "review_routing_view", lambda *_a, **_k: {})
    monkeypatch.setattr(private_view, "_session_summaries", lambda *_a: {})
    monkeypatch.setattr(factory_landing, "rollout_waits", lambda *_a, **_k: [])
    # Real status -> private board projection, with no task/graph hydration.
    with Session(world[0]) as db:
        board = private_view.build_factory_view(session=db)
    assert board["backlog_refill"]["status"] == "armed"
    refill.observe(policy, eligible=0, complete=True)
    with Session(world[0]) as db:
        board = private_view.build_factory_view(session=db)
    assert board["backlog_refill"]["status"] == "in flight"


def test_real_intake_count_precedes_capacity_and_excludes_other_candidates(
    world, monkeypatch
):
    from factory.orchestration import work_items

    engine, policy, _clock = world
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    repo = policy["repo"]

    def issue(number, labels=("agent-ready",), **extra):
        return {
            "number": number,
            "title": f"factory: issue {number}",
            "body": "body",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "state": "open",
            "assignees": [],
            "labels": list(labels),
            "created_at": NOW.isoformat(),
            **extra,
        }

    issues = [issue(number) for number in range(10, 34)]
    issues += [
        issue(100, labels=("agent-ready", "human")),
        issue(101, assignees=[{"login": "owner"}]),
        issue(102, labels=("agent-ready", "needs-human")),
        issue(103, labels=("needs-thought",)),
        issue(104),
        issue(105),
        issue(106, pull_request={}),
    ]
    # Prior delivery is excluded, and a linked PR owns #104.
    with Session(engine) as db:
        db.add(
            FactoryReceipt(
                repo=repo,
                issue_number=105,
                actor="test",
                state="succeeded",
                title="delivered",
                body="",
                url=f"https://github.com/{repo}/issues/105",
            )
        )
        # Fill delivery capacity while advisory remains open. These rows must not
        # make the 24 surviving delivery issues disappear from the refill count.
        db.add_all(
            [
                FactoryReceipt(
                    repo=repo,
                    issue_number=number,
                    actor="test",
                    state="admitted",
                    title="busy",
                    body="",
                    url=f"https://github.com/{repo}/issues/{number}",
                )
                for number in (200, 201)
            ]
        )
        db.commit()
    calls = []

    def pages(slug, endpoint, **_kwargs):
        calls.append((slug, endpoint))
        return (
            issues
            if endpoint == "issues"
            else [{"number": 500, "title": "owns #104", "body": ""}]
        ), False

    observed = []
    monkeypatch.setattr(intake, "_pages", pages)
    monkeypatch.setattr(
        refill, "observe", lambda _policy, **kwargs: observed.append(kwargs)
    )
    intake.intake_tick(policy, generation=policy["generation"])
    assert calls == [(repo, "issues"), (repo, "pulls")]
    assert observed[0]["eligible"] == 24
    assert observed[0]["complete"] is True


def test_refinement_batch_cannot_start_refill(world, monkeypatch):
    from factory.orchestration import work_items

    engine, policy, _clock = world
    update(
        world,
        issue_numbers=[],
        intake={"enabled": True, "refine_enabled": True, "refinement_batch": [300]},
    )
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    issues = [
        {
            "number": number,
            "title": f"factory: issue {number}",
            "body": "body",
            "html_url": f"https://github.com/{policy['repo']}/issues/{number}",
            "state": "open",
            "assignees": [],
            "labels": ["agent-ready"],
            "created_at": NOW.isoformat(),
        }
        for number in range(10, 40)
    ]
    monkeypatch.setattr(
        intake,
        "_pages",
        lambda _repo, endpoint, **_kw: (issues if endpoint == "issues" else [], False),
    )
    assert intake.intake_tick(policy, generation=policy["generation"]) == []
    with controls._read_session() as db:
        assert not refill._events(db, "started")
    assert (
        refill.observe(policy, eligible=0, complete=True)["reason"]
        == "refinement_batch"
    )


def test_refinement_batch_fences_previously_queued_refill(world):
    from factory.orchestration.factory_intake import _eligible_clause

    _engine, policy, _clock = world
    start = refill.observe(policy, eligible=0, complete=True)
    update(
        world,
        intake={"enabled": True, "refine_enabled": True, "refinement_batch": [300]},
    )
    assert not controls.admission_eligible(
        policy,
        refill.CHARTER,
        refill.ACTOR,
        task_class="advisory-diagnosis",
        repo=policy["repo"],
    )
    with controls._read_session() as db:
        assert not db.exec(
            select(FactoryReceipt).where(
                FactoryReceipt.id == start["receipt_id"], _eligible_clause(policy)
            )
        ).all()
    update(world, intake={"enabled": True, "refine_enabled": True})
    assert controls.admission_eligible(
        policy,
        refill.CHARTER,
        refill.ACTOR,
        task_class="advisory-diagnosis",
        repo=policy["repo"],
    )
    with controls._read_session() as db:
        assert (
            db.exec(
                select(FactoryReceipt).where(
                    FactoryReceipt.id == start["receipt_id"], _eligible_clause(policy)
                )
            )
            .one()
            .id
            == start["receipt_id"]
        )
    assert refill.observe(policy, eligible=0, complete=True)["reason"] == "in_flight"


def test_non_due_intake_ticks_do_not_observe_refill(world, monkeypatch):
    engine, policy, clock = world
    monkeypatch.setattr(intake, "_pages", lambda *_a, **_kw: ([], False))
    observed = []
    monkeypatch.setattr(
        refill, "observe", lambda _policy, **kwargs: observed.append(kwargs)
    )
    intake.intake_tick(policy, generation=policy["generation"])
    assert len(observed) == 1 and observed[0]["complete"]
    with Session(engine) as db:
        swept = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == "intake_swept")
        ).one()
        clock[0] = intake.aware(swept.created_at) + timedelta(seconds=15)
    assert not intake._listing_due(clock[0])
    intake.intake_tick(policy, generation=policy["generation"])
    assert len(observed) == 1


@pytest.mark.parametrize("failure", ["partial", "error"])
def test_intake_unknown_listing_cannot_trigger_refill(world, monkeypatch, failure):
    _engine, policy, _clock = world

    def pages(_repo, endpoint, **_kwargs):
        if failure == "error":
            raise TimeoutError("listing failed")
        return [], endpoint == "issues"

    observed = []
    monkeypatch.setattr(intake, "_pages", pages)
    monkeypatch.setattr(
        refill, "observe", lambda _policy, **kwargs: observed.append(kwargs)
    )
    intake.intake_tick(policy, generation=policy["generation"])
    assert observed and observed[0]["complete"] is False


def test_worker_graph_uses_one_pinned_read_only_node(world, monkeypatch):
    from types import SimpleNamespace

    from factory.orchestration import backlog_refill_worker as worker
    from factory.orchestration import factory_conductor as conductor

    engine, policy, _clock = world
    start = refill.observe(policy, eligible=0, complete=True)
    legacy_start(world, start)
    with Session(engine) as db:
        db.add(
            SwarmTask(
                id="t-refill",
                task_text="inventory",
                repo=policy["repo"],
                conductor_model="opus",
                created_at=NOW,
            )
        )
        db.flush()
        row = db.get(FactoryReceipt, start["receipt_id"])
        row.task_id, row.state = "t-refill", "admitted"
        row.policy_json = dumps(policy)
        db.commit()
    added = []

    def add(*args, **kwargs):
        added.append((args, kwargs))
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(conductor, "_add", add)
    monkeypatch.setattr(conductor, "_record_allowance", lambda *_args: None)
    worker.reconcile({"id": "t-refill"}, policy, [], [], 7)
    assert len(added) == 1
    args, kwargs = added[0]
    assert args[2] == refill.NODE and args[5] == "astra"
    assert kwargs["max_attempts"] == 1 and kwargs["effort"] == "high"
    assert conductor._schema(refill.NODE)["x-factory-scout-read-only"] is True
    worker.reconcile({"id": "t-refill"}, policy, [{"node_key": refill.NODE}], [], 8)
    assert len(added) == 1
    assert refill.dispatch_allowed("t-refill", "conductor_funding_1") is False
    assert refill.dispatch_allowed("t-refill", refill.NODE, 2) is False
    from factory.orchestration.api import factory_session_allowed

    with Session(engine) as db:
        db.add(
            SwarmNodeRun(
                task_id="t-refill",
                node_key=refill.NODE,
                attempt=1,
                status="admitted",
                model="astra",
                pin_json=dumps(
                    {
                        "task_id": "t-refill",
                        "node_key": refill.NODE,
                        "attempt": 1,
                        "model": "astra",
                    }
                ),
            )
        )
        db.commit()
    identity = f"factory:t-refill:{refill.NODE}:1"
    assert factory_session_allowed(identity)
    assert not factory_session_allowed("factory:t-refill:conductor_funding_1:1")
    assert not factory_session_allowed(f"factory:t-refill:{refill.NODE}:2")
    with Session(engine) as db:
        run = db.exec(select(SwarmNodeRun)).one()
        run.model = "opus"
        db.commit()
    assert not factory_session_allowed(identity)


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("source_state", ["open", "closed"])
def test_native_refill_refuses_generic_funding(world, monkeypatch, source_state):
    """Neither an open charter nor ordinary gate admission grants this profile."""
    from factory.orchestration import (
        factory_conductor as conductor,
    )
    from factory.orchestration import (
        factory_funding as funding,
    )
    from factory.orchestration import (
        factory_problem_issues as producer,
    )
    from factory.orchestration.api import factory_session_allowed

    engine, policy, _clock = world
    for module in (conductor, conductor.graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    start = refill.observe(policy, eligible=0, complete=True)
    task_id = admit(world, start)
    task = conductor._task(task_id)
    assert task["issue_number"] == 7366
    issue = {
        "number": 7366,
        "state": source_state,
        "title": "Native recurring refill charter",
        "body": "Inspect the backlog under the fixed refill contract.",
        "labels": [],
        "assignees": [],
    }
    monkeypatch.setattr(conductor, "github_get", lambda *_args: issue)
    monkeypatch.setattr(conductor, "github_list", lambda *_args: [])

    # Real native ownership is sufficient locally; the producer still rejects
    # the closed charter at its fresh GitHub boundary before publishing repair.
    source = FactoryAudit(
        actor=funding.ACTOR, action="recurring_blocker", task_id=task_id
    )
    with controls._read_session() as db:
        assert producer._recovery_owner(db, source)["issue_number"] == 7366
    context = producer._recovery_context(policy, source)
    assert context is None  # No synchronized, exact charter mirror.

    assert not funding.request(task, "Assess a bounded infrastructure repair retry")
    with controls._read_session() as db:
        request = funding.pending(db, task_id)
        assert request is None
        assert funding.amendment(db, task_id) is None
    assert controls.task_snapshot(task_id)["state"] == "admitted"
    assert not conductor.graph.load_graph(task_id)
    assert not factory_session_allowed(f"factory:{task_id}:conductor_funding_1:1")
    assert not refill.dispatch_allowed(task_id, refill.NODE, 2)


@pytest.fixture
def repair_loop(world, monkeypatch):
    """Real owners and native ledgers; only GitHub, DBOS and guest I/O are fake."""
    import json
    from types import SimpleNamespace

    import core.db

    from factory.execution import admission, result_receipts, store, transport
    from factory.execution import mcp as executor
    from factory.execution.models import (
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from factory.orchestration import backlog_refill_worker as worker
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_funding as funding
    from factory.orchestration import factory_problem_issues as producer
    from factory.orchestration import node_workflows

    engine, policy, clock = world
    engine.update_execution_options(
        schema_translate_map={"swarm": None, "agent_sessions": None}
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                AgentSession,
                AgentTurn,
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentResultReceipt,
                PendingMessage,
            )
        ],
    )
    for module in (
        core.db,
        admission,
        result_receipts,
        store,
        conductor,
        conductor.graph,
        executor,
    ):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(executor, "_schedule_next_message", lambda _: None)

    async def notify(*_args, **_kwargs):
        pass

    monkeypatch.setattr(executor, "_notify_terminal", notify)
    monkeypatch.setenv("FACTORY_CONDUCTOR_FUNDING_ENABLED", "true")
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    monkeypatch.setattr(producer, "_recovery_quota", lambda: True)
    update(
        world,
        problem_issues={"enabled": True, "sources": {"recurring_blocker": True}},
        intake={"enabled": True, "refine_enabled": True, "labels": ["agent-ready"]},
        backlog_refill={"enabled": True, "model": "opus", "max_findings": 5},
    )
    issues = {
        7366: {
            "number": 7366,
            "state": "closed",
            "title": "Native refill charter",
            "body": "Recurring inventory",
            "labels": [],
            "assignees": [],
        }
    }

    def get(_repo, path):
        if path.startswith("issues/"):
            return issues[int(path.split("/")[1])]
        return {"sha": "a" * 40, "object": {"sha": "a" * 40}}

    def create(_repo, payload):
        number = 8000 + len(issues)
        issue = {**payload, "number": number, "state": "open", "assignees": []}
        issues[number] = issue
        return issue

    monkeypatch.setattr(conductor, "github_get", get)
    monkeypatch.setattr(conductor, "github_list", lambda *_: [])
    monkeypatch.setattr(producer, "github_list", lambda *_: [])
    monkeypatch.setattr(producer, "github_write", create)
    monkeypatch.setattr(
        producer,
        "_discover",
        lambda _r, marker, _b: (
            [n for n, issue in issues.items() if marker in issue.get("body", "")],
            False,
        ),
    )
    monkeypatch.setattr(
        node_workflows, "_read_reconciliation_head", lambda *_: "a" * 40
    )
    producer.problem_issues_tick(
        policy
    )  # Fresh activation watermark precedes failures.
    with Session(engine) as db:
        item = WorkItem(
            title="Native refill charter",
            body="Recurring inventory",
            state="closed",
            close_reason="completed",
            source_kind="github",
            trust="trusted",
            authority="github",
            github_repo=policy["repo"],
            github_issue_number=7366,
        )
        db.add(item)
        db.commit()
        item_id = item.id

    def run_worker(task_id):
        worker.reconcile(
            conductor._task(task_id),
            policy,
            conductor.graph.load_graph(task_id),
            conductor.graph.node_runs(task_id),
            conductor.graph.current_version(task_id),
        )

    def dispatch(task_id, key):
        existing = conductor.graph.node_runs(task_id, key)
        if (
            existing
            and existing[-1]["status"] not in conductor.graph.TERMINAL_RUN_STATUSES
        ):
            return existing[-1]
        if key.startswith(funding.PREFIX):
            from factory.orchestration import backlog_refill_repair

            assert backlog_refill_repair.reconcile(
                conductor._task(task_id),
                policy,
                conductor.graph.node_runs(task_id),
                controls.can_start(task_id),
            )
        else:
            assert conductor._dispatch_ready(
                conductor._task(task_id),
                conductor.graph.load_graph(task_id),
                conductor.graph.node_runs(task_id),
                1,
                fan_out=False,
                parallel=1,
                policy=policy,
            )
        return conductor.graph.node_runs(task_id, key)[-1]

    def native(run, value=None, failure=None, profile_change=None):
        import base64
        import hashlib

        pin = run["pin"]
        prompt = node_workflows._node_prompt(
            pin["prompt"],
            pin["artifact_path"],
            pin["artifact_schema"],
            pin["retry_context"],
            pin["branch"],
        )
        with Session(engine) as db:
            agent = AgentSession(
                **node_workflows._expected_session_identity(pin),
                workspace="guest",
                admission_tier="project",
                ember_session_id=f"guest-{run['id']}",
                ember_session_token="synthetic-guest-token",
            )
            db.add(agent)
            db.flush()
            sid = agent.id
            db.add(
                PendingMessage(
                    session_id=sid, seq=1, message_text=prompt, model=pin["model"]
                )
            )
            db.commit()
        owner = f"executor-{sid}"
        assert conductor.graph.record_dispatch(
            pin["task_id"], pin["node_key"], pin["attempt"], sid, None
        ).ok
        profile = pin["artifact_schema"].get("x-factory-funding-decision")
        if profile is None:
            assert store.claim_pending_message_for_session_sync(sid, owner) == 1
            assert admission.recheck(sid, 1, owner, "claude-runtime")
        if failure is not None:
            store.mark_turn_error_sync(
                sid,
                1,
                "Typed fixture infrastructure failure",
                owner,
                invocation_not_attempted=True,
                dispatch_count=1,
                preinvoke_database_failure=failure,
            )
            result = {
                "status": "uncertain",
                "session_id": sid,
                "cost_usd": None,
                "reason": "terminal error requires native proof",
            }
        else:
            raw = {
                "result": dumps(value),
                "terminal_reason": "completed",
                "is_error": False,
                "total_cost_usd": 0.1,
                "num_turns": 1,
                "usage": {},
                "artifact": {
                    "path": pin["artifact_path"],
                    "outcome": "ok",
                    "content_b64": base64.b64encode(dumps(value).encode()).decode(),
                },
            }
            if profile:
                raw["usage"]["execution_profile"] = profile
                if profile_change == "missing":
                    raw["usage"].pop("execution_profile")
                elif profile_change == "mismatch":
                    raw["usage"]["execution_profile"] = {
                        **profile,
                        "request_sha256": "f" * 64,
                    }
            guest = f"guest-{run['id']}"
            if profile:
                import asyncio

                requests = []

                class Client:
                    def __init__(self, **_kwargs):
                        pass

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *_args):
                        pass

                    async def post(self, url, **kwargs):
                        request = httpx.Request("POST", url, **kwargs)
                        requests.append(request)
                        body = json.loads(request.content)
                        assert (
                            request.headers["X-Ember-Guest-Path"]
                            == "/shim/funding-decision-v1"
                        )
                        assert (
                            body["execution_profile"] == profile
                            and body["turn_seq"] == 1
                        )
                        assert not any(
                            k in body
                            for k in ("repo", "branch", "system_prompt", "push_format")
                        )
                        credential = body["result_receipt"]
                        result_receipts.capture_result(
                            credential["id"], credential["token"], dumps(raw).encode()
                        )
                        return httpx.Response(200, json=raw, request=request)

                with monkeypatch.context() as patch:
                    patch.setattr(transport.httpx, "AsyncClient", Client)
                    patch.setattr(
                        transport, "EMBERVM_URL", "https://synthetic-guest.invalid"
                    )
                    patch.setattr(
                        executor, "_transport", transport.EmberVmShimTransport()
                    )
                    asyncio.run(
                        asyncio.wait_for(executor._execute_pending_message(sid), 10)
                    )
                with Session(engine) as db:
                    assert len(requests) == 1, (
                        [
                            t.result_text
                            for t in db.exec(
                                select(AgentTurn).where(AgentTurn.session_id == sid)
                            )
                        ],
                        [
                            p.model_dump()
                            for p in db.exec(
                                select(PendingMessage).where(
                                    PendingMessage.session_id == sid
                                )
                            )
                        ],
                    )
            else:
                body = dumps({"message": prompt}).encode()
                envelope = result_receipts.prepare_receipt(sid, owner, 1, guest, body)
                result_receipts.capture_result(
                    envelope["id"], envelope["token"], dumps(raw).encode()
                )
                captured = result_receipts.read_active_result(
                    envelope["id"],
                    sid,
                    owner,
                    1,
                    guest,
                    hashlib.sha256(body).hexdigest(),
                )
                parsed = transport.parse_native_turn(
                    json.loads(captured["result_body"]),
                    guest,
                    artifact_path=pin["artifact_path"],
                )._replace(
                    native_receipt={
                        **captured["provenance"],
                        "cli_session_id": None,
                        "artifact_path": pin["artifact_path"],
                    }
                )
                store.persist_turn_from_pending_sync(
                    sid,
                    1,
                    prompt,
                    parsed,
                    "done",
                    "completed",
                    None,
                    pin["model"],
                    owner,
                    1,
                )
                assert result_receipts.mark_response_observed(
                    envelope["id"], sid, owner, 1, guest
                )
            result = node_workflows.reconcile_completed_node(pin, sid)
            if profile_change:
                assert result is None
                artifact = node_workflows._read_turn_artifact(
                    sid, 1, pin["artifact_path"], pin["artifact_schema"]
                )
                assert artifact["status"] == "invalid"
                assert artifact["errors"] == ["funding execution profile mismatch"]
                return sid
            assert result is not None, raw
            assert result["status"] == "succeeded", result
        dbos = SimpleNamespace(
            get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
            retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: result),
        )
        conductor.reconcile_task(pin["task_id"], policy, dbos)
        if failure is None:
            conductor.reconcile_task(pin["task_id"], policy, dbos)
        return sid

    collection = {
        "status": "completed",
        "source_sha": "a" * 40,
        "summary": "Verified inventory",
        "findings": [
            {
                "key": "ready",
                "title": "factory: add missing regression",
                "body": "A focused test is missing at graph admission.",
            }
        ],
    }

    def start_run(role=refill.NODE):
        start = refill.observe(policy, eligible=0, complete=True)
        assert start["ok"], start
        task_id = admit(world, start)
        with Session(engine) as db:
            db.get(FactoryReceipt, start["receipt_id"]).work_item_id = item_id
            db.commit()
        run_worker(task_id)
        run = dispatch(task_id, refill.NODE)
        if role == refill.REVIEW_NODE:
            native(run, collection)
            run_worker(task_id)
            run = dispatch(task_id, refill.REVIEW_NODE)
        return start, task_id, run

    def start_failure(role=refill.NODE):
        start, task_id, run = start_run(role)
        native(
            run,
            failure={
                "version": 1,
                "phase": "receipt_prepare",
                "sqlstate": "42703",
                "attempted": False,
            },
        )
        run_worker(task_id)
        return start, task_id

    return SimpleNamespace(
        engine=engine,
        policy=policy,
        clock=clock,
        issues=issues,
        start_run=start_run,
        start_failure=start_failure,
        run_worker=run_worker,
        dispatch=dispatch,
        native=native,
        funding=funding,
        collection=collection,
    )


@pytest.fixture
def available_repair_loop(repair_loop, monkeypatch):
    """Prospective provider availability, explicitly before fresh enrollment."""
    from factory.orchestration import funding_profile

    monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", True)
    return repair_loop


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("role", [refill.NODE, refill.REVIEW_NODE])
@pytest.mark.parametrize("previously_available", [False, True])
def test_unavailable_repair_route_does_not_pause_or_block_next_refill(
    repair_loop, monkeypatch, role, previously_available
):
    from factory.orchestration import backlog_refill_repair as repair
    from factory.orchestration import backlog_refill_worker as worker
    from factory.orchestration import factory_problem_issues as producer
    from factory.orchestration import funding_profile

    s = repair_loop
    assert funding_profile.PROVIDER_ROUTE_AVAILABLE is False
    if previously_available:
        monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", True)
    for index in range(2):
        start, task_id, run = s.start_run(role)
        with controls._read_session() as db:
            event = refill.latest_event(db, "started", run_id=start["run_id"])
            original = event.detail_json
            assert ("repair_contract" in refill._detail(event)) == previously_available
        # In the prospective case, retain a real prior matching failure and a
        # second enrolled start, then lose provider availability before failure.
        if index == 1:
            monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", False)
        if not previously_available:
            # A later supported route cannot enroll a cycle already started
            # without a repair contract.
            monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", True)
            assert not repair.execution_failure_eligible(f"factory:{task_id}:{role}:1")
            monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", False)
        if not funding_profile.PROVIDER_ROUTE_AVAILABLE:
            assert not repair.execution_failure_eligible(f"factory:{task_id}:{role}:1")
        s.native(
            run,
            failure={
                "version": 1,
                "phase": "receipt_prepare",
                "sqlstate": "42703",
                "attempted": False,
            },
        )
        s.run_worker(task_id)
        snapshot = controls.task_snapshot(task_id)
        assert snapshot["state"] == "failed"
        assert not snapshot["task_paused"]
        with controls._read_session() as db:
            assert (
                refill.latest_event(db, "started", run_id=start["run_id"]).detail_json
                == original
            )
            assert s.funding.latest(db, task_id, "repair_allocation_wait") is None
            assert s.funding.pending(db, task_id) is None
            assert s.funding.amendment(db, task_id) is None
            if not funding_profile.PROVIDER_ROUTE_AVAILABLE:
                assert (
                    s.funding.latest(db, task_id, "refill_infrastructure_failure")
                    is None
                )
        worker.tick(s.policy)
        with controls._read_session() as db:
            assert (
                refill.latest_event(db, "completed", run_id=start["run_id"]) is not None
            )
            assert not refill._ledger(db, s.policy["repo"])[2]
        s.clock[0] += timedelta(minutes=16)
    producer.problem_issues_tick(s.policy)
    assert set(s.issues) == {refill.CHARTER}
    # The second cycle started normally. Two completed failures still open the
    # ordinary breaker; do not reset it or leave an unfinishable repair wait.
    assert refill.observe(s.policy, eligible=0, complete=True) == {
        "ok": False,
        "reason": "breaker",
    }
    with controls._read_session() as db:
        assert (
            refill._detail(refill.latest_event(db, "breaker_open"))["reason"]
            == "failed_runs"
        )
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == s.funding.BLOCKER)
        ).all()


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("previously_available", [False, True])
def test_unavailable_repair_route_retains_ordinary_preinvoke_capacity_wait(
    repair_loop, monkeypatch, previously_available
):
    import asyncio

    from factory.execution import admission, store, transport
    from factory.execution import mcp as executor
    from factory.execution.models import AgentSession, AgentTurn
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import funding_profile, node_workflows

    s = repair_loop
    assert funding_profile.PROVIDER_ROUTE_AVAILABLE is False
    s.clock[0] = datetime.now(UTC)
    if previously_available:
        monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", True)
    _start, task_id, run = s.start_run()
    monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", False)
    monkeypatch.setenv("FACTORY_ASYNC_RESIDENCY_ENABLED", "true")
    pin = run["pin"]
    deadline = int((s.clock[0] + timedelta(hours=2)).timestamp() * 1000)
    with Session(s.engine) as db:
        agent = AgentSession(
            **node_workflows._expected_session_identity(pin),
            workspace="guest",
            admission_tier="project",
            ember_session_id="guest",
            ember_session_token="synthetic-guest-token",
            ember_lineage_id="synthetic-lineage",
            ember_session_expires_at=deadline,
        )
        db.add(agent)
        db.commit()
        sid = agent.id
        store.create_pending_message(db, sid, "synthetic refill", pin["model"])
    assert conductor.graph.record_dispatch(task_id, pin["node_key"], 1, sid, None).ok
    calls = []

    class PreinvokeFailure(transport.EmberVmShimTransport):
        async def get_session(self, guest, token):
            return {
                "session_id": guest,
                "lineage_id": "synthetic-lineage",
                "generation": 1,
                "expires_at": deadline,
            }

        async def residency_request(self, guest, token, method, **kwargs):
            calls.append(method)
            return {
                "id": "synthetic-residency",
                "session_id": guest,
                "generation": 1,
                "state": "ready",
                "reason": "capacity",
                "residency_epoch": "epoch",
            }

        async def deliver(self, *_args, **kwargs):
            await kwargs["admission_check"]()
            calls.append("preinvoke_failure")
            kwargs["invocation_record"].update(
                attempted=False,
                database_failure={
                    "version": 1,
                    "phase": "receipt_prepare",
                    "sqlstate": "42703",
                    "attempted": False,
                },
            )
            raise transport.EmberTurnNotInvoked("synthetic receipt preparation failure")

    monkeypatch.setattr(executor, "_transport", PreinvokeFailure())
    asyncio.run(asyncio.wait_for(executor._execute_pending_message(sid), 10))
    assert calls == ["POST", "preinvoke_failure", "DELETE"]
    with Session(s.engine) as db:
        pending = store.get_pending_message(db, sid, 1)
        assert pending.capacity_state == "waiting_capacity"
        assert pending.claimed_by_replica is None
        assert pending.dispatch_count == 0
        assert pending.capacity_started_at is None
        assert pending.message_text == "synthetic refill"
        permit = admission.reservation(db, db.get(AgentSession, sid).local_session_id)
        assert permit.state == "reserved"
        assert permit.owner is None
        assert not db.exec(select(AgentTurn).where(AgentTurn.session_id == sid)).all()
        assert s.funding.latest(db, task_id, "repair_allocation_wait") is None
    assert not controls.task_snapshot(task_id)["task_paused"]


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
def test_unavailable_repair_route_preserves_existing_machine_hold(
    available_repair_loop, monkeypatch
):
    from factory.orchestration import backlog_refill_repair as repair
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import funding_profile

    s = available_repair_loop
    first, _first_id = s.start_failure()
    refill.settle(first["run_id"], success=False, new_issues=0, summary="first failure")
    s.clock[0] += timedelta(minutes=16)
    _second, task_id = s.start_failure()
    before = controls.task_snapshot(task_id)
    assert before["task_paused"]
    with controls._read_session() as db:
        wait = s.funding.latest(db, task_id, "repair_allocation_wait")
        assert wait is not None
        events = [
            (row.id, row.detail_json) for row in db.exec(select(FactoryAudit)).all()
        ]
    monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", False)
    assert not repair.failed(conductor._task(task_id), refill.NODE)
    assert repair.reconcile(
        conductor._task(task_id),
        s.policy,
        conductor.graph.node_runs(task_id),
        controls.can_start(task_id),
    )
    assert controls.task_snapshot(task_id) == before
    with controls._read_session() as db:
        assert s.funding.latest(db, task_id, "repair_allocation_wait") == wait
        assert [
            (row.id, row.detail_json) for row in db.exec(select(FactoryAudit)).all()
        ] == events


@pytest.fixture
def repaired_refill(available_repair_loop, request, monkeypatch):
    """Prospective provider + post-operator boundary; neither is enabled live."""
    from factory.orchestration import factory_intake as intake_owner
    from factory.orchestration import factory_landing as landing
    from factory.orchestration import factory_problem_issues as producer

    s = available_repair_loop
    s.failed_role = getattr(request, "param", refill.NODE)
    first, first_id = s.start_failure(s.failed_role)
    assert controls.task_snapshot(first_id)["state"] == "failed"
    refill.settle(
        first["run_id"], success=False, new_issues=0, summary="Typed preinvoke failure"
    )
    s.clock[0] += timedelta(minutes=16)
    second, task_id = s.start_failure(s.failed_role)
    assert controls.task_snapshot(task_id)["task_paused"]
    with controls._read_session() as db:
        wait = s.funding.repair_pause_evidence(db, task_id)
        assert wait is not None
        assert wait["grant_id"] is None
    producer.problem_issues_tick(s.policy)
    assert len(s.issues) == 2, s.issues
    repair_issue = next(issue for n, issue in s.issues.items() if n != 7366)
    assert "needs-human" not in repair_issue["labels"]
    intake_owner.receive_issue(
        s.policy["repo"],
        repair_issue["number"],
        repair_issue["title"],
        repair_issue["body"],
        "https://github.com/jomcgi-org/homelab/issues/8001",
        intake_owner.INTAKE_ACTOR,
        task_class="bug-fix",
        issue=repair_issue,
    )
    admitted = intake_owner.admit_next("scheduler")
    assert admitted["ok"], admitted
    repair_id = admitted["task_id"]
    s.clock[0] += timedelta(
        hours=2
    )  # Real repair may outlive the original task's soft lease.
    # These execution owners are outside standing landing authority. This fixture
    # models an explicitly authorized repair's existing merge/rollout observations;
    # it tests downstream funding/retry only, not automatic landing eligibility.
    landing._record(repair_id, "merged", pr_number=8002, merge_commit_sha="a" * 40)
    s.funding.repair_recovery_tick()
    with controls._read_session() as db:
        assert s.funding.pending(db, task_id) is None
    landing._record(
        repair_id,
        "rollout_verified",
        pr_number=8002,
        merge_commit_sha="a" * 40,
        approved_head_sha="b" * 40,
        verified=True,
    )
    controls.finish_task(
        repair_id,
        "succeeded",
        "factory:landing",
        evidence={"state": "rollout_verified"},
    )
    from factory.orchestration import funding_profile

    monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", False)
    before = controls.task_snapshot(task_id)
    s.funding.repair_recovery_tick()
    assert controls.task_snapshot(task_id) == before
    with controls._read_session() as db:
        assert s.funding.pending(db, task_id) is None
    # Exercise staged downstream contracts with a hypothetical supported route.
    # The production default and independent guest refusal stay closed.
    monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", True)
    s.funding.repair_recovery_tick()
    with controls._read_session() as db:
        request = s.funding.pending(db, task_id)
        assert request is not None
    s.task_id, s.start, s.request, s.repair_id = task_id, second, request, repair_id
    return s


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
def test_native_funding_disabled_fences_preexisting_request(
    repaired_refill, monkeypatch
):
    from factory.orchestration import funding_profile

    s = repaired_refill
    judge = s.dispatch(s.task_id, s.request["node_key"])
    identity = f"factory:{s.task_id}:{s.request['node_key']}:1"
    assert funding_profile.session_profile(identity) is not None
    monkeypatch.setattr(funding_profile, "PROVIDER_ROUTE_AVAILABLE", False)
    with controls._read_session() as db:
        assert not funding_profile.allowed(
            db, s.task_id, s.request["node_key"], 1, pin=judge["pin"]
        )
        assert not s.funding._repair_review_valid(
            db, s.task_id, s.request["repair_recovery"]
        )
    assert funding_profile.session_profile(identity) is None
    before = controls.task_snapshot(s.task_id)
    assert not s.funding.request(
        {"id": s.task_id}, "disabled route", retry_request=s.request
    )
    assert controls.task_snapshot(s.task_id) == before


@pytest.mark.parametrize(
    "change",
    [
        {"phase": "after_invoke"},
        {"phase": []},
        {"sqlstate": "42501"},
        {"sqlstate": {}},
        {"attempted": True},
        {"attempted": None},
        {"version": True},
    ],
)
def test_native_repair_public_context_refuses_unknown_or_permission_failures(change):
    from factory.execution.preinvoke_failure import repair_context

    failure = {"phase": "receipt_prepare", "sqlstate": "42703", "attempted": False}
    assert repair_context(failure | change, refill.NODE) is None
    assert repair_context(failure, "funding_judge") is None
    assert repair_context(failure, []) is None
    context = repair_context(
        failure | {"sql": "private", "exception": "private", "session_id": 123},
        refill.NODE,
    )
    assert set(context) == {"role", "stage", "sqlstate", "owning_paths"}
    assert "private" not in json.dumps(context)


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("role", [refill.NODE, refill.REVIEW_NODE])
def test_native_repair_ticket_carries_only_authenticated_owner_context(
    available_repair_loop, role
):
    from factory.orchestration import factory_problem_issues as producer
    from factory.orchestration import repair_landing_delegation as delegation

    s = available_repair_loop
    first, first_id = s.start_failure(role)
    refill.settle(first["run_id"], success=False, new_issues=0, summary="Typed failure")
    s.clock[0] += timedelta(minutes=16)
    second, task_id = s.start_failure(role)
    producer.problem_issues_tick(s.policy)
    issue = next(issue for n, issue in s.issues.items() if n != 7366)
    body = issue["body"]
    with controls._read_session() as db:
        signal = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.task_id == task_id,
                FactoryAudit.action == s.funding.BLOCKER,
            )
        ).one()
        detail = json.loads(signal.detail_json)
        context = detail["preinvoke_repair"]
        assert context == {
            "role": "collector" if role == refill.NODE else "independent reviewer",
            "stage": "receipt_prepare",
            "sqlstate": "42703",
            "owning_paths": [
                "projects/monolith/factory/execution/result_receipts.py",
                "projects/monolith/factory/execution/result_receipts_test.py",
            ],
        }
        for field in (
            context["role"],
            context["stage"],
            context["sqlstate"],
            *context["owning_paths"],
        ):
            assert field in body
        for private in (task_id, first_id, second["run_id"], first["run_id"]):
            assert private not in body
        assert "factory_funding.py and factory_progress_watchdog.py" not in body
        assert "explicit operator-owned exact-head authorization is required" in body
        assert all(
            not set(context["owning_paths"]) & paths
            for paths in delegation.PATH_PROFILES.values()
        )
        # A forged public projection cannot be used as a public source of truth.
        detail["preinvoke_repair"]["owning_paths"] = ["private/sql/secret"]
        assert (
            producer._issue(
                "recurring_blocker", signal, detail, s.policy["repo"], 7366, "marker"
            )
            is None
        )


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize(
    "repaired_refill", [refill.NODE, refill.REVIEW_NODE], indirect=True
)
def test_native_refill_after_operator_repair_funding_retry_and_useful_delivery(
    repaired_refill,
):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_problem_issues as producer
    from factory.orchestration import funding_profile
    from factory.orchestration.api import factory_session_allowed

    s = repaired_refill
    task_id, second, request = s.task_id, s.start, s.request
    judge = s.dispatch(task_id, request["node_key"])
    identity = f"factory:{task_id}:{request['node_key']}:1"
    assert factory_session_allowed(identity)
    assert (
        funding_profile.session_profile(identity)
        == judge["pin"]["artifact_schema"][funding_profile.KEY]
    )
    assert not judge["pin"].get("read_only")
    assert not refill.dispatch_allowed(task_id, request["node_key"], physical=True)
    decision = {
        "action": "continue",
        "reason": "The exact repair rolled out.",
        "next_plan": "Retry the failed inspection, then independently review findings.",
        "task_budget_usd": 20.0,
        "additional_work_turns": 4,
        "lease_minutes": 30,
        "assessment": {
            "progress": "Repair verified",
            "proximity": "Inspection remains",
            "value": "Resume useful backlog work",
        },
    }
    s.native(judge, decision)
    with controls._read_session() as db:
        grant = s.funding.amendment(db, task_id)
        assert grant and grant["node_retry"]["after"] == 2, s.funding.latest(
            db, task_id, "funding_review_settled"
        )
    assert not factory_session_allowed(identity)
    with controls._read_session() as db:
        before_replay = s.funding.amendment(db, task_id)
    completed_judge = conductor.graph.node_runs(task_id, request["node_key"])[-1]
    s.funding.settle(conductor._task(task_id), completed_judge, request)
    with controls._read_session() as db:
        assert s.funding.amendment(db, task_id) == before_replay
    s.clock[0] += timedelta(minutes=3)  # More than a single role's turn timeout.
    s.run_worker(task_id)
    retry = s.dispatch(task_id, s.failed_role)
    assert retry["attempt"] == 2
    assert retry["pin"]["read_only"] is True
    collection = s.collection
    if s.failed_role == refill.NODE:
        s.native(retry, collection)
    producer.problem_issues_tick(s.policy)
    with controls._read_session() as db:
        assert not s.funding.latest(db, task_id, "problem_issue_recovery_observed")
    s.run_worker(task_id)
    reviewer = s.dispatch(task_id, refill.REVIEW_NODE)
    assert reviewer["pin"]["read_only"] is True
    verdict = {
        "status": "completed",
        "source_sha": "a" * 40,
        "collection_digest": refill.digest(collection),
        "summary": "Independently verified",
        "decisions": [
            {
                "key": "ready",
                "approved": True,
                "evidence": "Confirmed missing test",
                "plan": "Add a focused graph regression and run its suite.",
                "acceptance": "The regression fails before the fix and passes after.",
            }
        ],
    }
    s.native(reviewer, verdict)
    s.run_worker(task_id)
    assert controls.task_snapshot(task_id)["state"] == "succeeded"
    producer.problem_issues_tick(s.policy)
    with controls._read_session() as db:
        assert s.funding.latest(db, task_id, "problem_issue_recovery_observed")
        done = refill._detail(
            refill.latest_event(db, "completed", run_id=second["run_id"])
        )
        assert done["new_issues"] == 1
    assert not refill.dispatch_allowed(task_id, s.failed_role, 3)
    assert len(conductor.graph.node_runs(task_id, s.failed_role)) == 2


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("profile_change", ["missing", "mismatch"])
def test_native_funding_ingestion_requires_exact_runtime_profile(
    repaired_refill, profile_change
):
    s = repaired_refill
    judge = s.dispatch(s.task_id, s.request["node_key"])
    s.native(judge, {"action": "continue"}, profile_change=profile_change)
    with controls._read_session() as db:
        assert s.funding.amendment(db, s.task_id) is None
    assert not refill.dispatch_allowed(s.task_id, s.failed_role, 2)


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize(
    "change",
    [
        "human",
        "security",
        "needs-human",
        "pause",
        "unknown",
        "policy",
        "restore",
        "historical",
        "rollout",
        "request",
        "schema",
        "model",
        "prompt",
        "expired",
    ],
)
def test_native_funding_physical_fence_rechecks_live_authority(repaired_refill, change):
    import json

    from factory.orchestration import funding_profile
    from factory.orchestration.api import factory_session_allowed

    s = repaired_refill
    run = s.dispatch(s.task_id, s.request["node_key"])
    identity = f"factory:{s.task_id}:{s.request['node_key']}:1"
    assert factory_session_allowed(identity)
    with Session(s.engine) as db:
        row = controls._receipt(db, s.task_id)
        if change in {"human", "security", "needs-human"}:
            db.get(WorkItem, row.work_item_id).labels = [change]
        elif change == "pause":
            row.task_paused = True
        elif change == "unknown":
            row.state = "uncertain"
        elif change == "policy":
            live = db.get(FactoryControl, "factory")
            policy = json.loads(live.policy_json)
            policy["backlog_refill"]["enabled"] = False
            live.policy_json = dumps(policy)
        elif change == "restore":
            # Effective settings unchanged; a disable/re-enable audit still invalidates authority.
            controls._audit(
                db, "operator", "problem_issues_control_applied", enabled=False
            )
            controls._audit(
                db, "operator", "problem_issues_control_applied", enabled=True
            )
        elif change == "historical":
            started = refill.latest_event(db, "started", run_id=s.start["run_id"])
            detail = refill._detail(started)
            detail.pop("repair_contract")
            started.detail_json = dumps(detail)
        elif change == "rollout":
            proof = s.request["repair_recovery"]
            rollout = db.get(FactoryAudit, proof["rollout_audit_id"])
            detail = json.loads(rollout.detail_json)
            detail["merge_commit_sha"] = "c" * 40
            rollout.detail_json = dumps(detail)
        elif change == "request":
            requested = db.get(FactoryAudit, s.request["audit_id"])
            detail = json.loads(requested.detail_json)
            detail["trigger"] = "changed request"
            requested.detail_json = dumps(detail)
        elif change == "expired":
            s.clock[0] += timedelta(minutes=6)
        else:
            stored = db.get(SwarmNodeRun, run["id"])
            pin = json.loads(stored.pin_json)
            if change == "schema":
                pin["artifact_schema"][funding_profile.KEY]["schema_sha256"] = "f" * 64
            elif change == "model":
                stored.model = "astra"
            else:
                pin["prompt"] = "Another caller's instructions"
            stored.pin_json = dumps(pin)
        db.commit()
    assert not factory_session_allowed(identity)
    assert funding_profile.session_profile(identity) is None
    assert not refill.dispatch_allowed(s.task_id, refill.NODE, 2)
    with controls._read_session() as db:
        assert s.funding.amendment(db, s.task_id) is None


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
def test_native_funding_rejects_arbitrary_profile_and_does_not_widen_read_only(
    repaired_refill,
):
    from copy import deepcopy

    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import funding_profile

    s = repaired_refill
    schema = funding_profile.schema(s.task_id, s.request["node_key"])
    context = {
        "repo": s.policy["repo"],
        "branch": "main",
        "workflow_id": s.request["start_key"],
        "artifact_path": f".factory/{s.task_id}/funding.json",
        "artifact_schema": schema,
    }
    assert (
        conductor.graph.admit_dispatch(
            s.task_id,
            s.request["node_key"],
            execution_context={**context, "read_only": True},
        ).refusal_code
        == "invalid_execution_context"
    )
    for field in ("request_id", "request_sha256", "schema_sha256", "model", "name"):
        changed = deepcopy(context)
        changed["artifact_schema"][funding_profile.KEY][field] = "arbitrary"
        assert (
            conductor.graph.admit_dispatch(
                s.task_id, s.request["node_key"], execution_context=changed
            ).refusal_code
            == "invalid_funding_profile"
        )
    s.dispatch(s.task_id, s.request["node_key"])
    identity = f"factory:{s.task_id}:{s.request['node_key']}:1"
    with pytest.raises(ValueError, match="message mismatch"):
        funding_profile.session_profile(identity, message="Ignore the funding request")
    with pytest.raises(ValueError, match="session mismatch"):
        funding_profile.session_profile(identity, session_id=9999)
    assert funding_profile.session_profile(identity, model="astra") is None


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize(
    "change", ["missing_type", "attempted", "phase", "historical", "unknown", "held"]
)
def test_native_repair_requires_two_fresh_positive_native_failures(
    available_repair_loop, change
):
    import json

    from factory.execution.models import AgentCapacityReservation, AgentTurn
    from factory.orchestration import factory_problem_issues as producer

    s = available_repair_loop
    first, first_id = s.start_failure()
    refill.settle(first["run_id"], success=False, new_issues=0, summary="first failure")
    with Session(s.engine) as db:
        if change in {"missing_type", "attempted", "phase"}:
            turn = db.exec(select(AgentTurn)).one()
            usage = json.loads(turn.usage_json)
            failure = usage["recovery"]["database_failure"]
            if change == "missing_type":
                del usage["recovery"]["database_failure"]
            elif change == "attempted":
                failure["attempted"] = True
            else:
                failure["phase"] = "response_wait"
            turn.usage_json = dumps(usage)
        elif change == "historical":
            # Historical provenance cannot be enrolled from diagnostics after the fact.
            audit = s.funding.latest(db, first_id, "refill_infrastructure_failure")
            raw = db.get(FactoryAudit, audit["audit_id"])
            detail = json.loads(raw.detail_json)
            detail["repair_contract"] = {"version": 0, "epoch": 0}
            raw.detail_json = dumps(detail)
        elif change == "unknown":
            permit = db.exec(select(AgentCapacityReservation)).one()
            permit.state, permit.outcome = "uncertain", "response_lost"
        else:
            row = controls._receipt(db, first_id)
            db.get(WorkItem, row.work_item_id).labels = ["human"]
        db.commit()
    s.clock[0] += timedelta(minutes=16)
    if change == "held":
        assert refill.observe(s.policy, eligible=0, complete=True) == {
            "ok": False,
            "reason": "charter_human_owned",
        }
        return
    _second, task_id = s.start_failure()
    with controls._read_session() as db:
        assert s.funding.latest(db, task_id, "repair_allocation_wait") is None
        assert s.funding.amendment(db, task_id) is None
    producer.problem_issues_tick(s.policy)
    assert not any(
        "factory-problem" in issue.get("body", "") for issue in s.issues.values()
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_refill_real_reservation_preserves_read_only_pin(world, monkeypatch, legacy):
    from factory.orchestration import backlog_refill_worker as worker
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import node_workflows

    engine, policy, _clock = world
    for module in (conductor, conductor.graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    start = refill.observe(policy, eligible=0, complete=True)
    if legacy:
        legacy_start(world, start)
    task_id = admit(world, start)
    task = conductor._task(task_id)
    worker.reconcile(task, policy, [], [], 0)
    key = f"factory-node:{task_id}:{refill.NODE}:1"
    result = conductor.reserve_node(
        task_id,
        refill.NODE,
        key,
        {
            "repo": policy["repo"],
            "branch": "main",
            "workflow_id": key,
            "artifact_path": f".factory/{task_id}/collection.json",
            "artifact_schema": conductor._schema(refill.NODE, task_id),
            "read_only": True,
        },
    )
    assert result, result.refusal_code
    [run] = conductor.graph.node_runs(task_id)
    pin = node_workflows._validate_pin(run["pin"])
    assert pin["read_only"] is True
    assert pin["artifact_schema"] == (
        refill.SCHEMA if legacy else refill.COLLECT_SCHEMA
    )
    assert pin["model"] == normalize(policy)["model"]
    assert controls.task_snapshot(task_id)["starts"][0]["model"] == pin["model"]
    context = {
        key: value
        for key, value in run["pin"].items()
        if key in conductor.graph._CONTEXT_FIELDS
    }
    changed = conductor.graph.admit_dispatch(
        task_id, refill.NODE, dispatch_key=key, execution_context=context
    )
    assert changed.refusal_code == "dispatch_key_conflict"
    for unrelated in (
        "scout_collect_1",
        "scout_review_1",
        "implement_browser_capture_1",
        "review_dependency_1",
    ):
        refused = conductor.graph.admit_dispatch(
            task_id, unrelated, execution_context={**context, "read_only": True}
        )
        assert refused.refusal_code == "invalid_execution_context"


@pytest.fixture
def reviewed_run(world, github, monkeypatch, request):
    from types import SimpleNamespace

    from factory.execution.models import AgentSession
    from factory.orchestration import backlog_refill_worker as worker
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import node_workflows

    engine, policy, clock = world
    engine.update_execution_options(
        schema_translate_map={"swarm": None, "agent_sessions": None}
    )
    SQLModel.metadata.create_all(engine, tables=[AgentSession.__table__])
    for module in (conductor, conductor.graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setattr(conductor, "_free_background_slots", lambda: 3)
    monkeypatch.setattr(
        conductor,
        "github_get",
        lambda *_: {"sha": "a" * 40, "object": {"sha": "a" * 40}},
    )
    monkeypatch.setattr(conductor, "github_list", lambda *_: [])
    update(world, backlog_refill={"enabled": True, "model": "opus", "max_findings": 5})
    start = refill.observe(policy, eligible=0, complete=True)
    task_id = admit(world, start)
    task = conductor._task(task_id)
    with controls._read_session() as db:
        detail = refill._detail(
            refill.latest_event(db, "started", run_id=start["run_id"])
        )
    checked = []
    monkeypatch.setattr(
        refill, "quota", lambda model, *, force=True: checked.append(model) or {}
    )

    def reconcile():
        worker.reconcile(
            task,
            policy,
            conductor.graph.load_graph(task_id),
            conductor.graph.node_runs(task_id),
            conductor.graph.current_version(task_id),
        )

    def dispatch(node_key):
        checked.clear()
        assert conductor._dispatch_ready(
            task,
            conductor.graph.load_graph(task_id),
            conductor.graph.node_runs(task_id),
            1,
            fan_out=False,
            parallel=1,
            policy=policy,
        )
        [run] = conductor.graph.node_runs(task_id, node_key)
        pin = node_workflows._validate_pin(run["pin"])
        assert checked == ["opus" if node_key == refill.NODE else "astra"]
        assert pin["read_only"] is True
        assert "Do not modify source" in node_workflows._node_prompt(
            pin["prompt"],
            pin["artifact_path"],
            pin["artifact_schema"],
            branch=pin["branch"],
        )
        assert refill.dispatch_allowed(task_id, node_key, physical=True)
        assert len(checked) == 2
        return run

    def settle(run, artifact):
        pin = node_workflows._validate_pin(run["pin"])
        identity = node_workflows._expected_session_identity(pin)
        with Session(engine) as db:
            session = AgentSession(
                **identity, workspace="/workspace", admission_tier="background"
            )
            db.add(session)
            db.flush()
            session_id = session.id
            db.commit()
        assert conductor.graph.record_dispatch(
            task_id, run["node_key"], 1, session_id, None
        ).ok
        assert conductor.graph.record_outcome(
            task_id,
            run["node_key"],
            1,
            "succeeded",
            0.1,
            None,
            dumps({"value": artifact}),
        ).ok
        assert controls.record_start_outcome(
            task_id,
            pin["workflow_id"],
            "succeeded",
            "test-worker",
            cost_usd=0.1,
            session_id=session_id,
        )["ok"]

    collection = {
        "summary": "Repository inventory with evidence.",
        "findings": [
            {
                "key": "ready",
                "title": "factory: verify refill dispatch",
                "body": "Reproduced at graph.py:1366.",
            },
            {
                "key": "duplicate",
                "title": "factory: duplicate tracked work",
                "body": "Already tracked.",
            },
        ],
    }
    maximum = getattr(request, "param", None) == "maximum"
    if maximum:
        collection = {
            "summary": "\U0001f9ea" * 12000,
            "findings": [
                {
                    "key": f"finding-{index}",
                    "title": f"factory: finding {index}",
                    "body": '"' * 3000,
                }
                for index in range(5)
            ],
        }
    reconcile()
    collector = dispatch(refill.NODE)
    if "inspection_contract" in detail:
        collection.update(status="completed", source_sha="a" * 40)
    scenario = getattr(request, "param", None)
    if scenario == "preflight":
        return SimpleNamespace(
            engine=engine,
            task_id=task_id,
            task=task,
            policy=policy,
            start=detail,
            collector=collector,
            reconcile=reconcile,
            github=github,
        )
    if scenario in {"empty", "blocked", "source_mismatch"}:
        collection["findings"] = []
    if scenario == "blocked":
        collection.update(status="blocked", source_sha=None)
    if scenario == "source_mismatch":
        collection["source_sha"] = "b" * 40
    settle(collector, collection)
    reconcile()
    if scenario in {"blocked", "source_mismatch"}:
        return SimpleNamespace(
            engine=engine,
            task_id=task_id,
            start=detail,
            collection=collection,
            collector=collector,
            reconcile=reconcile,
            github=github,
        )
    assert writer.advance(detail) is None and not github[1]
    reviewer = dispatch(refill.REVIEW_NODE)
    assert reviewer["pin"]["model"] == "astra" and reviewer["pin"]["effort"] == "xhigh"
    assert conductor.graph.load_graph(task_id)[-1]["deps"] == [refill.NODE]
    verdict = {
        "collection_digest": refill.digest(collection),
        "summary": "Verified the failure; rejected the existing issue.",
        "decisions": [
            {
                "key": "ready",
                "approved": True,
                "evidence": "The current focused test fails at graph admission.",
                "plan": "Edit graph.py and its focused test. Revert the PR if unrelated dispatch changes.",
                "acceptance": "Run pytest backlog_refill_test.py; expect every test to pass.",
            },
            {
                "key": "duplicate",
                "approved": False,
                "evidence": "An existing issue owns this work.",
                "plan": "Wait for the current owner.",
                "acceptance": "Require a distinct reproducible defect.",
            },
        ],
    }
    if maximum:
        verdict["summary"] = "r" * 12000
        verdict["decisions"] = [
            {
                "key": finding["key"],
                "approved": True,
                **{
                    field: field[0] * 3000
                    for field in ("evidence", "plan", "acceptance")
                },
            }
            for finding in collection["findings"]
        ]
    if "inspection_contract" in detail:
        verdict.update(status="completed", source_sha="a" * 40)
    if scenario == "empty":
        verdict["decisions"] = []
    if scenario == "review_blocked":
        verdict.update(status="blocked", source_sha=None, decisions=[])
    settle(reviewer, verdict)
    return SimpleNamespace(
        engine=engine,
        policy=policy,
        clock=clock,
        task_id=task_id,
        start=detail,
        collector=collector,
        reviewer=reviewer,
        verdict=verdict,
        collection=collection,
        reconcile=reconcile,
        checked=checked,
        settle=settle,
        github=github,
    )


def test_independent_refill_review_precedes_every_new_issue(reviewed_run):
    state = reviewed_run
    assert not state.github[1]
    for _ in range(3):
        state.reconcile()
    # Routine runs publish only findings; the summary stays in the audit.
    assert len(state.github[1]) == 1
    (finding,) = state.github[0]
    assert finding["title"] == state.collection["findings"][0]["title"]
    assert "Implementation plan:" in finding["body"]
    assert "Acceptance criteria:" in finding["body"]
    assert "expect every test to pass" in finding["body"]
    with controls._read_session() as db:
        evidence = refill._detail(
            refill.latest_event(db, "reviewed", run_id=state.start["run_id"])
        )
        assert evidence["collector_session_id"] != evidence["reviewer_session_id"]
        assert evidence["collection_digest"] == refill.digest(state.collection)
        completed = refill._detail(
            refill.latest_event(db, "completed", run_id=state.start["run_id"])
        )
        assert completed["new_issues"] == 1 and completed["low_yield"]
    assert (
        refill.observe(state.policy, eligible=0, complete=True)["reason"] == "cooldown"
    )


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "missing_decision",
        "empty_plan",
        "same_session",
        "wrong_session_model",
        "wrong_pin_model",
        "wrong_start_model",
        "missing_start",
        "wrong_artifact",
    ],
)
def test_refill_review_refuses_unproven_provenance(reviewed_run, change):
    import json

    from factory.execution.models import AgentSession

    state = reviewed_run
    with Session(state.engine) as db:
        collector = db.get(SwarmNodeRun, state.collector["id"])
        reviewer = db.get(SwarmNodeRun, state.reviewer["id"])
        grant = db.exec(
            select(FactoryStart).where(FactoryStart.start_key == reviewer.dispatch_key)
        ).one()
        artifact = json.loads(reviewer.outcome_json)["value"]
        if change == "digest":
            artifact["collection_digest"] = "0" * 64
        elif change == "missing_decision":
            artifact["decisions"].pop()
        elif change == "empty_plan":
            artifact["decisions"][0]["plan"] = " "
        elif change == "same_session":
            reviewer.session_id = grant.session_id = collector.session_id
        elif change == "wrong_session_model":
            db.get(AgentSession, reviewer.session_id).model = "opus"
        elif change == "wrong_pin_model":
            pin = json.loads(reviewer.pin_json)
            pin["model"] = "opus"
            reviewer.pin_json = dumps(pin)
        elif change == "wrong_start_model":
            grant.model = "opus"
        elif change == "missing_start":
            db.delete(grant)
        elif change == "wrong_artifact":
            collector.outcome_json = dumps(
                {"value": {"summary": "changed", "findings": []}}
            )
        reviewer.outcome_json = dumps({"value": artifact})
        db.commit()
    with pytest.raises(ValueError):
        writer.review(state.start["run_id"], state.reviewer["id"])
    assert writer.advance(state.start) is None
    assert not state.github[1]


@pytest.mark.parametrize("change", ["pause", "cancel", "expire", "policy"])
def test_reviewed_refill_rechecks_owner_after_discovery(
    reviewed_run, monkeypatch, change
):
    state = reviewed_run
    writer.review(state.start["run_id"], state.reviewer["id"])
    discover = writer.producer._discover

    def race(*args):
        if change == "expire":
            state.clock[0] += timedelta(seconds=state.policy["task_timeout_seconds"])
        else:
            with Session(state.engine) as db:
                row = db.get(FactoryReceipt, state.start["receipt_id"])
                if change == "pause":
                    row.task_paused = True
                elif change == "cancel":
                    row.cancellation_requested = True
                else:
                    control = db.get(FactoryControl, "factory")
                    control.policy_json = dumps({**state.policy, "generation": 1})
                db.commit()
        return discover(*args)

    monkeypatch.setattr(writer.producer, "_discover", race)
    assert writer.advance(state.start) is None
    assert not state.github[1]


def test_refill_review_cannot_authorize_an_unreviewed_output(reviewed_run):
    state = reviewed_run
    writer.review(state.start["run_id"], state.reviewer["id"])
    for kind in ("finding", "summary"):
        assert (
            writer.publish(
                writer.output(
                    state.policy["repo"],
                    state.start["run_id"],
                    kind,
                    "injected",
                    "factory: unrelated output",
                    "Unreviewed prose",
                    ["agent-ready"],
                )
            )
            is None
        )
    assert not state.github[1]


def test_rejected_refill_findings_settle_as_zero_yield(reviewed_run):
    state = reviewed_run
    for decision in state.verdict["decisions"]:
        decision["approved"] = False
    with Session(state.engine) as db:
        db.get(SwarmNodeRun, state.reviewer["id"]).outcome_json = dumps(
            {"value": state.verdict}
        )
        db.commit()
    state.reconcile()
    assert state.github[1] == []
    with controls._read_session() as db:
        result = refill._detail(
            refill.latest_event(db, "completed", run_id=state.start["run_id"])
        )
        assert result["new_issues"] == 0 and result["low_yield"]


def test_failed_review_releases_run_without_publishing_collection(reviewed_run):
    from factory.orchestration import backlog_refill_worker as worker

    state = reviewed_run
    with Session(state.engine) as db:
        run = db.get(SwarmNodeRun, state.reviewer["id"])
        run.status = "failed"
        db.exec(
            select(FactoryStart).where(FactoryStart.start_key == run.dispatch_key)
        ).one().status = "failed"
        db.commit()
    state.reconcile()
    worker.tick(state.policy)
    assert not state.github[1]
    with controls._read_session() as db:
        assert refill.latest_event(db, "reviewed", run_id=state.start["run_id"]) is None
        result = refill._detail(
            refill.latest_event(db, "completed", run_id=state.start["run_id"])
        )
        assert result["success"] is False and result["new_issues"] == 0


@pytest.mark.parametrize(
    "node_key,model", [(refill.NODE, "opus"), (refill.REVIEW_NODE, "astra")]
)
@pytest.mark.parametrize("change", ["quota", "pause_during_read", "expire_during_read"])
def test_refill_role_dispatch_needs_fresh_quota_and_current_authority(
    world, monkeypatch, node_key, model, change
):
    engine, policy, clock = world
    update(world, backlog_refill={"enabled": True, "model": "opus"})
    start = refill.observe(policy, eligible=0, complete=True)
    task_id = admit(world, start)
    checked = []

    def read(chosen):
        checked.append(chosen)
        if change == "quota":
            raise ValueError("refill_subscription_unavailable")
        if change == "pause_during_read":
            with Session(engine) as db:
                db.get(FactoryReceipt, start["receipt_id"]).task_paused = True
                db.commit()
        else:
            clock[0] += timedelta(seconds=policy["task_timeout_seconds"])

    monkeypatch.setattr(refill, "quota", read)
    assert not refill.dispatch_allowed(task_id, node_key)
    assert checked == [model]
    with controls._read_session() as db:
        assert not db.exec(select(FactoryStart)).all()


@pytest.mark.parametrize("model,provider", [("opus", "claude"), ("astra", "codex")])
@pytest.mark.parametrize(
    "state", ["fresh", "stale", "unknown", "exhausted", "unoffered"]
)
def test_refill_quota_uses_fresh_exact_subscription(
    world, monkeypatch, model, provider, state
):
    from factory import execution
    from factory.execution import provider_quota

    monkeypatch.setattr(
        execution,
        "offered_models",
        lambda: [] if state == "unoffered" else ["opus", "astra"],
    )
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return {
            "available": True,
            "providers": {
                provider: {
                    "observed": state != "unknown",
                    "age_seconds": 100000 if state == "stale" else 0,
                    "windows": [
                        {
                            "name": "primary" if provider == "codex" else "5h",
                            "used_percent": 100 if state == "exhausted" else 1,
                        }
                    ],
                }
            },
        }

    monkeypatch.setattr(provider_quota, "fetch_provider_quota_sync", fetch)
    if state == "fresh":
        assert _REAL_REFILL_QUOTA(model)["provider"] == provider
    else:
        with pytest.raises(ValueError, match="refill_"):
            _REAL_REFILL_QUOTA(model)
    assert calls == ([] if state == "unoffered" else [{"force": True}])


def test_legacy_collection_is_explicitly_unreviewed(world):
    start = publication_start(world)
    writer.collection(
        start["run_id"], {"summary": "Historical inventory", "findings": []}
    )
    with controls._read_session() as db:
        historical = refill._detail(
            refill.latest_event(db, "started", run_id=start["run_id"])
        )
        collected = refill._detail(
            refill.latest_event(db, "collection", run_id=start["run_id"])
        )
        assert "review_contract" not in historical
        assert collected["review_status"] == "legacy_unreviewed"
        assert refill.latest_event(db, "reviewed", run_id=start["run_id"]) is None


@pytest.mark.parametrize("character", ['"', "\\", "\n", "\U0001f9ea"])
def test_new_refill_schema_maximum_fits_review_without_losing_evidence(character):
    import json

    from jsonschema import Draft202012Validator

    schema = refill.schema(refill.NODE)
    fields = schema["properties"]
    finding_fields = fields["findings"]["items"]["properties"]
    value = {
        "summary": character * fields["summary"]["maxLength"],
        "findings": [
            {
                "key": str(index).ljust(finding_fields["key"]["maxLength"], "k"),
                "title": "a" * 18 + ": " + '"' * 180,
                "body": character * finding_fields["body"]["maxLength"],
            }
            for index in range(fields["findings"]["maxItems"])
        ],
    }
    Draft202012Validator(schema).validate(value)
    prompt = refill.review_brief(value)
    assert len(refill.READ_ONLY_BOUNDARY + prompt) <= 60000
    assert json.loads(prompt.split("\n", 1)[1])["collection"] == value
    assert (
        fields["summary"]["maxLength"]
        + len("\n\nIndependent review:\n")
        + refill.REVIEW_SCHEMA["properties"]["summary"]["maxLength"]
        <= 60000
    )


@pytest.mark.parametrize(
    "summary_length,body_length,count", [(60000, 0, 0), (1000, 12000, 5), (55000, 0, 0)]
)
def test_new_collector_schema_rejects_evidence_too_large_for_review(
    world, summary_length, body_length, count
):
    from jsonschema import Draft202012Validator

    value = {
        "summary": "s" * summary_length,
        "findings": [
            {
                "key": f"finding-{index}",
                "title": f"factory: finding {index}",
                "body": "b" * body_length,
            }
            for index in range(count)
        ],
    }
    assert list(Draft202012Validator(refill.schema(refill.NODE)).iter_errors(value))
    assert not list(Draft202012Validator(refill.SCHEMA).iter_errors(value))
    start = refill.observe(world[1], eligible=0, complete=True)
    with pytest.raises(ValueError, match="invalid bounded refill collection"):
        writer.collection(start["run_id"], value)
    with controls._read_session() as db:
        assert refill.latest_event(db, "collection", run_id=start["run_id"]) is None


@pytest.mark.parametrize("reviewed_run", ["maximum"], indirect=True)
def test_maximum_new_collection_and_review_publish_without_truncation(reviewed_run):
    state = reviewed_run
    for _ in range(6):
        state.reconcile()
    assert len(state.github[0]) == 5
    for original, published in zip(state.collection["findings"], state.github[0]):
        assert published["body"].startswith(original["body"])
        for field in ("evidence", "plan", "acceptance"):
            assert field[0] * 3000 in published["body"]
    with controls._read_session() as db:
        completed = refill._detail(
            refill.latest_event(db, "completed", run_id=state.start["run_id"])
        )
        assert completed["new_issues"] == 5 and completed["success"] is True


@pytest.mark.parametrize("codepoint", [0, 1, 11, 12, 31, 0xD800, 0xDFFF])
def test_new_collection_schema_refuses_text_outside_escaping_bound(codepoint):
    from jsonschema import Draft202012Validator

    assert list(
        Draft202012Validator(refill.COLLECT_SCHEMA).iter_errors(
            {"summary": chr(codepoint), "findings": []}
        )
    )


@pytest.mark.parametrize(
    "expired,disable_on_expiry", [(False, False), (True, False), (True, True)]
)
@pytest.mark.parametrize(
    "pause_source,unresolved",
    [
        (None, False),
        ("operator", False),
        ("refusal", False),
        ("operator", True),
        ("refusal", True),
    ],
)
def test_conductor_dispatch_and_expired_never_dispatched_refill(
    world, monkeypatch, expired, pause_source, unresolved, disable_on_expiry
):
    from types import SimpleNamespace

    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import factory_merged_delivery as merged

    engine, policy, clock = world
    start = refill.observe(policy, eligible=24, complete=True)
    with Session(engine) as db:
        db.add(
            SwarmTask(
                id="t-refill",
                task_text="inventory",
                repo=policy["repo"],
                conductor_model="opus",
                created_at=clock[0],
            )
        )
        db.flush()
        row = db.get(FactoryReceipt, start["receipt_id"])
        row.task_id, row.state = "t-refill", "admitted"
        row.policy_json = dumps(policy)
        if unresolved:
            db.add(
                FactoryStart(
                    task_id="t-refill",
                    start_key="unknown-refill",
                    actor=refill.ACTOR,
                    model="astra",
                    max_cost_usd=2,
                    status="uncertain",
                )
            )
        db.commit()
    if pause_source == "operator":
        assert controls.set_control("pause_task", "operator", task_id="t-refill")["ok"]
    elif pause_source == "refusal":
        conductor._pause_refusal({"id": "t-refill"}, "dispatch_refused: test refusal")
    if pause_source:
        assert controls.can_start("t-refill")["reason"] == "task_paused"
    if expired:
        if disable_on_expiry:
            update(
                world,
                generation=policy["generation"] + 1,
                backlog_refill={"enabled": False},
            )
        clock[0] += timedelta(seconds=policy["task_timeout_seconds"])
    monkeypatch.setattr(
        conductor, "_consume_intervention_notifications", lambda *_: None
    )
    monkeypatch.setattr(conductor, "_task", lambda *_: {"id": "t-refill"})
    monkeypatch.setattr(conductor.graph, "node_runs", lambda *_: [])
    monkeypatch.setattr(conductor.graph, "load_graph", lambda *_: [])
    monkeypatch.setattr(conductor.graph, "current_version", lambda *_: 0)
    monkeypatch.setattr(merged, "reconcile_merged_delivery", lambda *_: False)
    monkeypatch.setattr(conductor, "_add", lambda *_a, **_k: SimpleNamespace(ok=True))
    monkeypatch.setattr(conductor, "_record_allowance", lambda *_: None)
    dispatched = []

    # Keep the real dispatch signature: omitting fan_out must fail here just
    # as it does in production, without touching the model transport.
    def dispatch(task, nodes, runs, slots, *, fan_out, parallel, policy):
        dispatched.append((fan_out, slots))

    monkeypatch.setattr(conductor, "_dispatch_ready", dispatch)
    conductor.reconcile_task("t-refill", policy, object())
    if unresolved:
        from factory.orchestration import backlog_refill_worker as worker

        worker.tick(policy)
        assert refill.state(policy)["in_flight"] == [start["run_id"]]
        assert dispatched == []
        with Session(engine) as db:
            assert db.get(FactoryReceipt, start["receipt_id"]).state == "admitted"
            db.exec(select(FactoryStart)).one().status = "failed"
            db.commit()
        conductor.reconcile_task("t-refill", policy, object())
    with Session(engine) as db:
        row = db.get(FactoryReceipt, start["receipt_id"])
        assert row.state == ("failed" if expired else "admitted")
    assert dispatched == ([] if expired or pause_source else [(False, 1)])
    if expired:
        from factory.orchestration import backlog_refill_worker as worker

        worker.tick(policy)
        assert refill.state(policy)["in_flight"] == []
        update(world, backlog_refill={"enabled": True})
        assert refill.observe(policy, eligible=24, complete=True)["ok"]


def test_cancelled_run_retains_uncertain_output_fence(world, github, monkeypatch):
    from factory.orchestration import backlog_refill_worker as worker

    engine, policy, clock = world
    start = publication_start(world)
    with controls._read_session() as db:
        detail = refill._detail(refill._events(db, "started")[0])
    writer.collection(
        start["run_id"],
        {
            "summary": "inventory",
            "findings": [
                {
                    "key": "key",
                    "title": "factory: test",
                    "body": "command and acceptance",
                }
            ],
        },
    )

    def lost(*_args):
        raise TimeoutError("ambiguous POST")

    monkeypatch.setattr(writer.producer, "github_write", lost)
    assert writer.advance(detail) is None
    with Session(engine) as db:
        db.get(FactoryReceipt, start["receipt_id"]).state = "cancelled"
        db.commit()
    update(world, backlog_refill={"enabled": False})
    clock[0] += timedelta(minutes=2)
    worker.tick(policy)
    with controls._read_session() as db:
        assert not refill._events(db, "completed")
    assert refill.state(policy)["in_flight"] == [start["run_id"]]


@pytest.mark.parametrize("change", ["pause", "cancel", "expire", "generation"])
def test_publication_rechecks_owner_after_discovery(world, github, monkeypatch, change):
    engine, policy, clock = world
    start = publication_start(world)
    item = writer.output(
        policy["repo"],
        start["run_id"],
        "finding",
        "race",
        "factory: discovery race",
        "evidence",
        ["agent-ready"],
    )
    discover = writer.producer._discover

    def race(*args):
        if change == "expire":
            clock[0] += timedelta(seconds=policy["task_timeout_seconds"])
        elif change == "generation":
            update(world, generation=1)
        else:
            with Session(engine) as db:
                row = db.get(FactoryReceipt, start["receipt_id"])
                if change == "pause":
                    row.task_paused = True
                else:
                    row.cancellation_requested = True
                db.commit()
        return discover(*args)

    monkeypatch.setattr(writer.producer, "_discover", race)
    assert writer.publish(item) is None
    assert not github[1]
    with controls._read_session() as db:
        assert writer._last(db, item["output_id"]) is None


def test_exhausted_publication_releases_run_and_late_reconciliation_is_read_only(
    world, github, monkeypatch
):
    from factory.orchestration import backlog_refill_worker as worker

    engine, policy, clock = world
    start = publication_start(world)
    with controls._read_session() as db:
        detail = refill._detail(
            refill.latest_event(db, "started", run_id=start["run_id"])
        )
    findings = [
        {"key": key, "title": f"factory: {key}", "body": "evidence"}
        for key in ("ambiguous", "healthy")
    ]
    writer.collection(start["run_id"], {"summary": "inventory", "findings": findings})
    create = writer.producer.github_write
    attempts = []

    def write(repo, payload):
        attempts.append(payload)
        if payload["title"] == "factory: ambiguous":
            raise TimeoutError("POST outcome unknown")
        return create(repo, payload)

    monkeypatch.setattr(writer.producer, "github_write", write)
    block = controls.problem_issues_policy({})
    monkeypatch.setattr(
        controls,
        "problem_issues_policy",
        lambda _policy: {**block, "retry_minutes": [1] * 6},
    )
    assert writer.advance(detail) is None
    for _ in range(6):
        clock[0] += timedelta(minutes=1)
        writer.advance(detail)
    outcome = writer.advance(detail)
    assert outcome["inconclusive"] and outcome["new_issues"] == 1
    assert len(attempts) == 2  # Ambiguous finding, healthy finding.
    with Session(engine) as db:
        db.get(FactoryReceipt, start["receipt_id"]).state = "failed"
        db.commit()
    refill.settle(start["run_id"], **outcome)
    assert refill.state(policy)["in_flight"] == []
    assert refill.state(policy)["breaker"] is None
    clock[0] = NOW + timedelta(minutes=15)
    next_start = publication_start(world)
    replay = writer.output(
        policy["repo"],
        next_start["run_id"],
        "finding",
        "new-model-key",
        "factory: ambiguous",
        "evidence",
        ["agent-ready"],
    )
    assert writer.publish(replay)["status"] == "quarantined"
    assert len(attempts) == 2
    # An operator reset/disable cannot erase the original no-repost fence.
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    update(world, backlog_refill={"enabled": False})
    github[0].append({**attempts[0], "number": 999})
    clock[0] += timedelta(hours=24)
    worker.tick(policy)
    with controls._read_session() as db:
        original = writer.output(
            policy["repo"],
            start["run_id"],
            "finding",
            "ambiguous",
            "factory: ambiguous",
            "evidence",
            ["agent-ready"],
        )
        reconciled = writer._last(db, original["output_id"])
        assert reconciled["status"] == "created" and reconciled["issue_number"] == 999
    worker.tick(policy)
    assert len(attempts) == 2


@pytest.mark.parametrize("interruption", ["generation", "pause"])
def test_policy_interruption_does_not_trip_failure_or_yield_breaker(
    world, interruption
):
    from factory.orchestration import backlog_refill_worker as worker

    engine, policy, clock = world
    for _ in range(3):
        start = refill.observe(policy, eligible=0, complete=True)
        assert start["ok"]
        if interruption == "pause":
            admit(world, start)
        with Session(engine) as db:
            row = db.get(FactoryReceipt, start["receipt_id"])
            row.state = "cancelled" if interruption == "generation" else "failed"
            row.task_paused = interruption == "pause"
            db.commit()
        if interruption == "generation":
            update(world, generation=policy["generation"] + 1)
        worker.tick(policy)
        clock[0] += timedelta(minutes=15)
    assert refill.state(policy)["breaker"] is None
    assert refill.state(policy)["status"] == "armed"
    with controls._read_session() as db:
        assert all(
            refill._detail(row)["inconclusive"]
            for row in refill._events(db, "completed")
        )


def test_transport_fence_does_not_load_audit_history(world, monkeypatch):
    start = publication_start(world)
    monkeypatch.setattr(
        refill, "_events", lambda *_: pytest.fail("unbounded audit read")
    )
    assert refill.dispatch_allowed("t-" + start["run_id"])


def test_receipt_identity_cannot_match_numeric_prefix(world):
    from factory.orchestration import backlog_refill_worker as worker

    engine, policy, _clock = world
    with Session(engine) as db:
        # Allocate the next receipt as 12 without relying on SQLite row order.
        db.add(
            FactoryReceipt(
                id=11,
                repo=policy["repo"],
                issue_number=11,
                title="other",
                body="",
                url="",
                actor="test",
                state="succeeded",
            )
        )
        db.commit()
    start = publication_start(world)
    assert start["receipt_id"] == 12
    with controls._locked_session() as (db, _control):
        detail = refill._detail(
            refill.latest_event(db, "started", run_id=start["run_id"])
        )
        refill.record(
            db,
            "started",
            **{
                **detail,
                "receipt_id": 123,
                "run_id": "other-run",
                "policy_digest": "other-policy",
            },
        )
    task_id = "t-" + start["run_id"]
    assert worker.receipt(task_id)["start"]["run_id"] == start["run_id"]
    assert refill.dispatch_allowed(task_id)


@pytest.fixture
def control_world(world, monkeypatch):
    import json

    from sqlalchemy import text

    from factory.execution.models import (
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from factory.orchestration import refill_control as api

    engine, _policy, clock = world
    engine.update_execution_options(
        schema_translate_map={"swarm": None, "agent_sessions": None}
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                AgentSession,
                AgentTurn,
                AgentCapacityPool,
                AgentCapacityReservation,
                PendingMessage,
            )
        ],
    )
    with Session(engine) as db:
        db.execute(
            text(
                "CREATE TABLE routine_jobs (name TEXT PRIMARY KEY,routine_kind TEXT,interval_secs INTEGER,next_run_at TIMESTAMP,last_run_at TIMESTAMP,last_status TEXT,locked_by TEXT,locked_at TIMESTAMP,payload TEXT)"
            )
        )
        db.execute(
            text(
                "INSERT INTO routine_jobs VALUES (:name,'qwen-drain',86400,:next,:last,'ok',NULL,NULL,:payload)"
            ),
            {
                "name": api.JOB,
                "next": NOW + timedelta(days=1),
                "last": NOW,
                "payload": json.dumps(
                    {
                        "learning_stage": "evidence",
                        "model": "luna",
                        "repo": api.learning.REPO,
                        "digest": "factory-retro",
                    }
                ),
            },
        )
        row = db.get(FactoryControl, "factory")
        p = json.loads(row.policy_json)
        p["backlog_refill"]["enabled"] = False
        row.policy_json = dumps(p)
        db.commit()
    monkeypatch.setenv("FACTORY_LEARNING_HANDOFF_ENABLED", "true")
    monkeypatch.setattr(
        api,
        "_quota",
        lambda model: {
            "provider": "codex",
            "observation": {"status": "allowed"},
            "checked_at": clock[0].isoformat(),
        },
    )
    return world


def test_safe_refill_control_minimal_diff_hourly_replay_and_history(control_world):
    import json

    from sqlalchemy import text

    from factory.orchestration import refill_control as api

    engine, _, _ = control_world
    with Session(engine) as db:
        before = json.loads(db.get(FactoryControl, "factory").policy_json)
        job_before = dict(
            db.execute(text("SELECT * FROM routine_jobs")).mappings().one()
        )
        refill.record(
            db, "breaker_open", repo=before["repo"], reason="low yield", runs=["old"]
        )
        db.commit()
    p = api.preview(True, "astra", True, 0, "hourly useful refill")
    r = api.apply(
        "operator",
        True,
        "astra",
        True,
        0,
        "hourly useful refill",
        "one",
        p["preview_sha256"],
    )
    assert (
        api.apply(
            "operator",
            True,
            "astra",
            True,
            0,
            "hourly useful refill",
            "one",
            p["preview_sha256"],
        )
        == r
    )
    with Session(engine) as db:
        after = json.loads(db.get(FactoryControl, "factory").policy_json)
        assert {k: v for k, v in before.items() if k != "backlog_refill"} == {
            k: v for k, v in after.items() if k != "backlog_refill"
        }
        assert (
            after["backlog_refill"]["enabled"]
            and after["backlog_refill"]["model"] == "astra"
            and after["backlog_refill"]["max_findings"] == 5
        )
        assert refill.state(after, session=db)["status"] == "breaker open"
        job_after = dict(
            db.execute(text("SELECT * FROM routine_jobs")).mappings().one()
        )
        assert job_after["interval_secs"] == 3600
        assert {
            k: v
            for k, v in job_after.items()
            if k not in ("interval_secs", "next_run_at")
        } == {
            k: v
            for k, v in job_before.items()
            if k not in ("interval_secs", "next_run_at")
        }
        assert len(db.execute(text("SELECT * FROM routine_jobs")).all()) == 1
        assert not db.exec(select(FactoryReceipt)).all()
        assert not db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "backlog_refill_policy_observed"
            )
        ).all()
    assert r["generation"] == before["generation"] and not r["dispatched"]


def test_refill_control_reset_breaker_rearms_without_generation_change(
    control_world,
):
    import json

    from factory.orchestration import refill_control as api

    engine, _, _ = control_world
    with Session(engine) as db:
        before = json.loads(db.get(FactoryControl, "factory").policy_json)
        refill.record(
            db, "breaker_open", repo=before["repo"], reason="low_yield", runs=["old"]
        )
        db.commit()
    args = (True, "opus", False, 0, "re-arm routines")
    plain = api.preview(*args)
    p = api.preview(*args, reset_breaker=True, min_new_issues=1)
    assert p["reset_breaker"] and p["preview_sha256"] != plain["preview_sha256"]
    assert p["refill_after"]["min_new_issues"] == 1
    # A digest previewed without the reset cannot apply one.
    with pytest.raises(ValueError, match="refill_preview_changed"):
        api.apply(
            "operator",
            *args,
            "wrong",
            plain["preview_sha256"],
            reset_breaker=True,
            min_new_issues=1,
        )
    call = ("operator", *args, "rearm", p["preview_sha256"])
    result = api.apply(*call, reset_breaker=True, min_new_issues=1)
    assert api.apply(*call, reset_breaker=True, min_new_issues=1) == result
    with pytest.raises(ValueError, match="refill_request_conflict"):
        api.apply(*call)
    with Session(engine) as db:
        after = json.loads(db.get(FactoryControl, "factory").policy_json)
        assert after["generation"] == before["generation"]
        assert after["backlog_refill"]["min_new_issues"] == 1
        assert refill.state(after, session=db)["breaker"] is None
        assert not db.exec(select(FactoryReceipt)).all()
    assert not result["dispatched"]


def test_refill_control_checks_both_roles_without_hourly_learning(
    control_world, monkeypatch
):
    from factory.orchestration import refill_control as api

    checked = []
    read = api._quota
    monkeypatch.setattr(
        api, "_quota", lambda model: checked.append(model) or read(model)
    )
    preview = api.preview(True, "opus", False, 0, "rank and challenge")
    assert checked == ["opus", "astra"]
    assert preview["new_run_review_contract"] == refill.REVIEW_CONTRACT
    checked.clear()
    result = api.apply(
        "operator",
        True,
        "opus",
        False,
        0,
        "rank and challenge",
        "dual-role",
        preview["preview_sha256"],
    )
    assert checked == ["opus", "astra"] and not result["dispatched"]


@pytest.mark.parametrize(
    "mutation",
    ["version", "policy", "locked", "held", "pending", "uncertain", "unknown"],
)
def test_refill_control_refuses_stale_or_executing_learning(control_world, mutation):
    import json

    from sqlalchemy import text

    from factory.execution.models import (
        AgentCapacityReservation,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from factory.orchestration import refill_control as api

    engine, _, _ = control_world
    p = api.preview(True, "astra", True, 0, "reason")
    with Session(engine) as db:
        c = db.get(FactoryControl, "factory")
        if mutation == "version":
            c.version += 1
        elif mutation == "policy":
            d = json.loads(c.policy_json)
            d["intake"]["cooldown_hours"] += 1
            c.policy_json = dumps(d)
        elif mutation == "locked":
            db.execute(text("UPDATE routine_jobs SET locked_by='owner'"))
        elif mutation == "held":
            db.execute(text("UPDATE routine_jobs SET next_run_at=NULL"))
        else:
            a = AgentSession(
                local_session_id="w:qwen-drain:" + api.JOB,
                workspace=".",
                branch="main",
                workflow_id="w",
                node_key="qwen-drain",
                status="completed",
            )
            db.add(a)
            db.flush()
            if mutation == "pending":
                db.add(PendingMessage(session_id=a.id, seq=2, message_text="pending"))
            elif mutation == "uncertain":
                db.add(
                    AgentCapacityReservation(
                        session_id=a.id,
                        local_session_id=a.local_session_id,
                        pending_seq=1,
                        tier="project",
                        state="uncertain",
                        routine_job_name=api.JOB,
                    )
                )
            else:
                db.add(
                    AgentTurn(
                        session_id=a.id,
                        seq=1,
                        prompt="",
                        result_text="",
                        terminal_reason="error",
                        stop_reason="invocation_outcome_unknown",
                    )
                )
        db.commit()
    with pytest.raises(ValueError):
        api.apply(
            "operator", True, "astra", True, 0, "reason", "key", p["preview_sha256"]
        )
    with Session(engine) as db:
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == api.ACTION)
        ).all()


def test_refill_control_replay_conflict_and_quota_refusal(control_world, monkeypatch):
    from factory.orchestration import refill_control as api

    p = api.preview(True, "astra", False, 0, "reason")
    api.apply("operator", True, "astra", False, 0, "reason", "key", p["preview_sha256"])
    with pytest.raises(ValueError, match="request_conflict"):
        api.apply(
            "operator", True, "astra", False, 0, "changed", "key", p["preview_sha256"]
        )
    monkeypatch.setattr(
        api,
        "_quota",
        lambda _: (_ for _ in ()).throw(ValueError("refill_subscription_unavailable")),
    )
    with pytest.raises(ValueError, match="subscription"):
        api.preview(True, "opus", False, 1, "reason")


def test_refill_status_exposes_bounded_policy_schedule_and_events(control_world):
    from factory.orchestration import refill_control as api

    engine, policy, _ = control_world
    with Session(engine) as db:
        for i in range(8):
            refill.record(
                db,
                "skipped",
                repo=policy["repo"],
                reason="cooldown",
                private_body="not returned",
            )
        db.commit()
        s = api.status(db, db.get(FactoryControl, "factory"))
        assert s["configured"] and not s["policy"]["enabled"]
        assert len(s["recent_events"]) == 5 and s["events_truncated"]
        assert all("private_body" not in e for e in s["recent_events"])
        assert s["learning_job"]["interval_secs"] == 86400
        assert s["intake_selection"][0]["issue_numbers"] == [1]


def test_hourly_learning_checks_codex_even_when_refill_disabled(
    control_world, monkeypatch
):
    from factory.orchestration import refill_control as api

    checked = []

    def refuse(model):
        checked.append(model)
        raise ValueError("refill_subscription_unavailable")

    monkeypatch.setattr(api, "_quota", refuse)
    with pytest.raises(ValueError, match="subscription"):
        api.preview(False, "opus", True, 0, "hourly")
    assert checked == ["astra"]
    assert api.preview(False, "opus", False, 0, "disable")["ok"]


def test_new_refill_batch_is_enforced_before_collection_audit(world):
    engine, policy, _ = world
    with Session(engine) as db:
        refill.record(
            db,
            "started",
            repo=policy["repo"],
            run_id="small",
            policy={"max_findings": 1},
        )
        db.commit()
    collection = {
        "summary": "Evidence",
        "findings": [
            {
                "key": str(i),
                "title": "fix: useful finding",
                "body": "Acceptance: verified.",
            }
            for i in range(2)
        ],
    }
    with pytest.raises(ValueError, match="pinned batch"):
        writer.collection("small", collection)
    with Session(engine) as db:
        assert refill.latest_event(db, "collection", run_id="small") is None


def test_disable_legacy_refill_keeps_unavailable_default_inert(control_world):
    import json

    from factory.orchestration import refill_control as api

    engine, _, _ = control_world
    with Session(engine) as db:
        control = db.get(FactoryControl, "factory")
        policy = json.loads(control.policy_json)
        policy.pop("backlog_refill")
        policy["allowed_models"] = ["opus"]
        control.policy_json = dumps(policy)
        db.commit()
    p = api.preview(False, "opus", False, 0, "disable")
    api.apply(
        "operator", False, "opus", False, 0, "disable", "legacy", p["preview_sha256"]
    )
    with Session(engine) as db:
        policy = json.loads(db.get(FactoryControl, "factory").policy_json)
        assert normalize(policy)["enabled"] is False
        assert "model" not in policy["backlog_refill"]
        assert (
            api.status(db, db.get(FactoryControl, "factory"))["policy"]["enabled"]
            is False
        )
    # A floor on a disabled legacy block still drops the unavailable default.
    floor = api.preview(False, "opus", False, 1, "floor", min_new_issues=1)
    assert "model" not in floor["refill_after"]
    assert floor["refill_after"]["min_new_issues"] == 1


def test_refill_control_reset_refuses_while_a_run_is_in_flight(control_world):
    import json

    from factory.orchestration import refill_control as api

    engine, _, _ = control_world
    with Session(engine) as db:
        repo = json.loads(db.get(FactoryControl, "factory").policy_json)["repo"]
        refill.record(db, "started", repo=repo, run_id="r1")
        db.commit()
    # The block itself is unchanged, so only the reset can trip the guard.
    args = (False, "opus", False, 0, "re-arm")
    assert api.preview(*args)["ok"]
    with pytest.raises(ValueError, match="refill_in_flight"):
        api.preview(*args, reset_breaker=True)


def test_refill_control_preserves_existing_active_uncertain_and_held_receipts(
    control_world,
):
    from factory.orchestration import refill_control as api

    engine, policy, _ = control_world
    with Session(engine) as db:
        db.add_all(
            [
                FactoryReceipt(
                    repo=policy["repo"],
                    issue_number=i,
                    actor="owner",
                    title="existing",
                    body="",
                    url="",
                    state=state,
                    task_paused=state == "escalated",
                    allowance_json='{"spent":4}',
                    direction_json='{"landing_hold":true}',
                    policy_json=dumps(policy),
                )
                for i, state in enumerate(
                    ("queued", "admitted", "uncertain", "escalated"), 20
                )
            ]
        )
        db.commit()
        before = [
            r.model_dump()
            for r in db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
        ]
    p = api.preview(True, "astra", False, 0, "enable")
    api.apply(
        "operator", True, "astra", False, 0, "enable", "preserve", p["preview_sha256"]
    )
    with Session(engine) as db:
        after = [
            r.model_dump()
            for r in db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
        ]
        assert after == before


@pytest.fixture
def learning_recovery_world(control_world, monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace

    from sqlalchemy import text

    from factory import learning_reconciliation as recovery
    from factory.execution.models import (
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        ProbeObservation,
    )
    from factory.execution.transport import EmberVmShimTransport

    engine, _, clock = control_world
    SQLModel.metadata.create_all(
        engine, tables=[AgentResultReceipt.__table__, ProbeObservation.__table__]
    )
    failed = NOW - timedelta(days=2)
    dispatched = failed - timedelta(minutes=5)
    started = int(dispatched.timestamp() * 1000) + 1000
    updated = int((failed + timedelta(minutes=10)).timestamp() * 1000)
    workflow = "_drainer-worker:1:1821"
    with Session(engine) as db:
        db.execute(
            text(
                "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY,status TEXT)"
            )
        )
        db.execute(
            text("INSERT INTO workflow_status VALUES (:id,'SUCCESS')"), {"id": workflow}
        )
        agent = AgentSession(
            local_session_id=f"{workflow}:qwen-drain:factory-retro-daily",
            workflow_id=workflow,
            node_key="qwen-drain",
            status="failed",
            admission_tier="project",
            model="luna",
            workspace=".",
            branch="main",
            created_at=dispatched,
            last_turn_at=failed,
        )
        db.add(agent)
        db.flush()
        turn = AgentTurn(
            session_id=agent.id,
            seq=1,
            prompt="retained private prompt",
            result_text="retained partial output",
            terminal_reason="error",
            stop_reason="invocation_outcome_unknown",
            created_at=failed,
            usage_json=dumps(
                {
                    "cost_usd": 1.25,
                    "recovery": {
                        "claim_owner": "old-owner",
                        "dispatch_count": 1,
                        "last_dispatch_at": dispatched.isoformat(),
                    },
                }
            ),
        )
        permit = AgentCapacityReservation(
            session_id=agent.id,
            local_session_id=agent.local_session_id,
            pending_seq=1,
            tier="project",
            routine_job_name="factory-retro-daily",
            state="settled",
            outcome="guest_cessation_confirmed",
            owner="old-owner",
            created_at=dispatched,
            settled_at=failed + timedelta(minutes=11),
        )
        db.add_all([turn, permit])
        db.flush()
        observation = ProbeObservation(
            permit_id=permit.id,
            identity_sha256="a" * 64,
            guest_id="old-guest",
            generation=0,
            invoke_started_at=started,
            cp_updated_at=updated,
            reason="guest_cessation_confirmed",
            checked_at=failed + timedelta(minutes=10),
            settled_at=failed + timedelta(minutes=11),
        )
        db.add(observation)
        ids = (agent.id, 1, permit.id)
        db.commit()
    observed = {
        "session_id": "old-guest",
        "generation": 0,
        "invoke_started_at": started,
        "updated_at": updated,
        "last_invoke_at": None,
        "state": "destroyed",
    }
    calls = []

    async def get_session(self, guest_id, session_token=None):
        calls.append(guest_id)
        return deepcopy(observed)

    monkeypatch.setattr(EmberVmShimTransport, "get_session", get_session)

    async def no_history(*args, **kwargs):
        raise RuntimeError("historical evidence unavailable")

    monkeypatch.setattr(
        EmberVmShimTransport, "get_session_cessation_evidence", no_history
    )
    return SimpleNamespace(
        engine=engine,
        clock=clock,
        ids=ids,
        observed=observed,
        calls=calls,
        recovery=recovery,
    )


def _learning_snapshot(state):
    from sqlalchemy import text

    from factory.execution.models import (
        AgentCapacityReservation,
        AgentSession,
        AgentTurn,
        ProbeObservation,
    )

    with Session(state.engine) as db:
        return {
            "agent": db.get(AgentSession, state.ids[0]).model_dump(),
            "turn": db.exec(
                select(AgentTurn).where(AgentTurn.session_id == state.ids[0])
            )
            .one()
            .model_dump(),
            "permit": db.get(AgentCapacityReservation, state.ids[2]).model_dump(),
            "observation": db.get(ProbeObservation, state.ids[2]).model_dump(),
            "job": dict(
                db.execute(text("SELECT * FROM routine_jobs")).mappings().one()
            ),
            "policy": db.get(FactoryControl, "factory").policy_json,
        }


@pytest.mark.asyncio
async def test_learning_reconciliation_only_unblocks_cadence_and_preserves_unknown(
    learning_recovery_world,
):
    from factory.execution import store
    from factory.orchestration import refill_control as api

    s = learning_recovery_world
    before = _learning_snapshot(s)
    with pytest.raises(ValueError, match="refill_learning_execution_unknown"):
        api.preview(True, "opus", True, 0, "activate")
    # Coupling is explicit: refill-only does not run the cadence guard. This is
    # a diagnostic regression test, not an alternative activation path.
    assert api.preview(True, "opus", False, 0, "inspect coupling")["refill_after"][
        "enabled"
    ]
    p = await s.recovery.preview(*s.ids, "Acknowledge ceased historical attempt")
    assert "retained private" not in dumps(p) and "retained partial" not in dumps(p)
    r = await s.recovery.apply(
        "operator",
        *s.ids,
        "Acknowledge ceased historical attempt",
        "stable",
        p["preview_sha256"],
    )
    assert (
        r["unknown_outcome_preserved"]
        and not r["dispatched"]
        and not r["cadence_changed"]
    )
    assert _learning_snapshot(s) == before
    assert r["version"] == 1
    with Session(s.engine) as db:
        with pytest.raises(store.SessionOutcomeUnknown):
            store._assert_sendable(db, s.ids[0])
        assert s.recovery.resolutions(db, {s.ids[0]}) == {s.ids[0]}
    assert api.preview(True, "opus", True, 1, "activate")["hourly"]


@pytest.mark.asyncio
async def test_learning_reconciliation_replays_lost_apply_before_remote_read(
    learning_recovery_world, monkeypatch
):
    from factory.execution.transport import EmberVmShimTransport

    s = learning_recovery_world
    p = await s.recovery.preview(*s.ids, "reason")
    args = ("operator", *s.ids, "reason", "stable", p["preview_sha256"])
    r = await s.recovery.apply(*args)

    async def unavailable(*args, **kwargs):
        pytest.fail("replay must not contact the guest")

    monkeypatch.setattr(EmberVmShimTransport, "get_session", unavailable)
    assert await s.recovery.apply(*args) == r
    with pytest.raises(ValueError, match="request_conflict"):
        await s.recovery.apply(
            "operator", *s.ids, "changed", "stable", p["preview_sha256"]
        )
    with Session(s.engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == s.recovery.ACTION)
                ).all()
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "running"),
        ("state", "parked"),
        ("state", "failed"),
        ("session_id", "different"),
        ("generation", 1),
        ("generation", True),
        ("invoke_started_at", 1),
        ("updated_at", 1),
        ("last_invoke_at", True),
    ],
)
async def test_learning_reconciliation_requires_exact_positive_cessation(
    learning_recovery_world, field, value
):
    s = learning_recovery_world
    s.observed[field] = value
    with pytest.raises(ValueError, match="learning_cessation_mismatch"):
        await s.recovery.preview(*s.ids, "reason")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["missing", "timeout", "gone", "http_404", "permission"]
)
async def test_learning_reconciliation_remote_failure_never_authorizes_apply(
    learning_recovery_world, monkeypatch, failure
):
    from factory.execution.models import ProbeObservation
    from factory.execution.transport import EmberSessionGone, EmberVmShimTransport

    s = learning_recovery_world
    with Session(s.engine) as db, db.begin():
        audit = db.get(ProbeObservation, s.ids[2])
        audit.evidence_json = dumps(
            {
                **s.observed,
                "terminal_state": s.observed["state"],
                "cessation_evidence": "terminal_dispatch",
                "observed_at": audit.checked_at.isoformat(),
                "response_sha256": "b" * 64,
            }
        )
        db.add(audit)
    original = _learning_snapshot(s)
    p = await s.recovery.preview(*s.ids, "reason")

    async def read(*args, **kwargs):
        if failure == "missing":
            return
        if failure == "gone":
            raise EmberSessionGone("gone")
        if failure == "http_404":
            response = httpx.Response(
                404, request=httpx.Request("GET", "https://guest.invalid")
            )
            response.raise_for_status()
        if failure == "permission":
            raise PermissionError("denied")
        raise TimeoutError("unavailable")

    monkeypatch.setattr(EmberVmShimTransport, "get_session", read)
    with pytest.raises(ValueError, match="cessation_unavailable"):
        await s.recovery.preview(*s.ids, "reason")
    with pytest.raises(ValueError, match="cessation_unavailable"):
        await s.recovery.apply(
            "operator", *s.ids, "reason", "stable", p["preview_sha256"]
        )
    with Session(s.engine) as db:
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == s.recovery.ACTION)
        ).all()
        assert db.get(FactoryControl, "factory").version == 0
    assert _learning_snapshot(s) == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "version",
        "policy",
        "job_held",
        "job_locked",
        "workflow",
        "pending",
        "binding",
        "cleanup",
        "receipt_fence",
        "permit",
        "job_identity",
        "observation",
        "turn",
        "extra_turn",
        "result_receipt",
        "new_owner",
        "new_work",
    ],
)
async def test_learning_reconciliation_rechecks_every_owner_before_commit(
    learning_recovery_world, change
):
    from sqlalchemy import text

    from factory.execution.models import (
        AgentCapacityReservation,
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
        ProbeObservation,
    )

    s = learning_recovery_world
    p = await s.recovery.preview(*s.ids, "reason")
    with Session(s.engine) as db:
        agent = db.get(AgentSession, s.ids[0])
        permit = db.get(AgentCapacityReservation, s.ids[2])
        if change == "version":
            db.get(FactoryControl, "factory").version += 1
        elif change == "policy":
            import json

            c = db.get(FactoryControl, "factory")
            policy = json.loads(c.policy_json)
            policy["generation"] += 1
            c.policy_json = dumps(policy)
        elif change == "job_held":
            db.execute(text("UPDATE routine_jobs SET next_run_at=NULL"))
        elif change == "job_locked":
            db.execute(text("UPDATE routine_jobs SET locked_by='worker'"))
        elif change == "workflow":
            db.execute(text("UPDATE workflow_status SET status='PENDING'"))
        elif change == "pending":
            db.add(PendingMessage(session_id=agent.id, seq=2, message_text="queued"))
        elif change == "binding":
            agent.ember_session_id = "old-guest"
        elif change == "cleanup":
            agent.guest_cleanup_id = "pending-cleanup"
        elif change == "receipt_fence":
            agent.result_receipt_fence_id = "late-receipt"
        elif change == "permit":
            permit.state = "uncertain"
        elif change == "job_identity":
            permit.routine_job_name = "other-job"
        elif change == "observation":
            db.get(ProbeObservation, permit.id).identity_sha256 = "b" * 64
        elif change == "turn":
            db.exec(
                select(AgentTurn).where(AgentTurn.session_id == agent.id)
            ).one().result_text = "late output"
        elif change == "extra_turn":
            db.add(
                AgentTurn(
                    session_id=agent.id, seq=2, prompt="new", result_text="unknown"
                )
            )
        elif change == "result_receipt":
            db.add(
                AgentResultReceipt(
                    id="late",
                    token_sha256="b" * 64,
                    session_id=agent.id,
                    local_session_id=agent.local_session_id,
                    seq=1,
                    dispatch_count=1,
                    claim_owner="old-owner",
                    guest_id="old-guest",
                    request_sha256="a" * 64,
                    created_at=NOW,
                    accept_until=NOW,
                    retain_until=NOW,
                )
            )
        else:
            other = AgentSession(
                local_session_id="other:qwen-drain:factory-retro-daily"
                if change == "new_work"
                else "other",
                workspace=".",
                branch="main",
                status="running",
                ember_session_id="old-guest" if change == "new_owner" else None,
            )
            db.add(other)
        db.commit()
    with pytest.raises(ValueError):
        await s.recovery.apply(
            "operator", *s.ids, "reason", "stable", p["preview_sha256"]
        )
    with Session(s.engine) as db:
        assert not db.exec(
            select(FactoryAudit).where(FactoryAudit.action == s.recovery.ACTION)
        ).all()


@pytest.mark.asyncio
async def test_learning_reconciliation_rechecks_after_remote_io_and_stale_evidence(
    learning_recovery_world, monkeypatch
):
    from factory.execution.models import PendingMessage
    from factory.execution.transport import EmberVmShimTransport

    s = learning_recovery_world
    p = await s.recovery.preview(*s.ids, "reason")
    candidate, _ = s.recovery._prepare(*s.ids, "reason")
    for delta in [-1, 31]:
        s.clock[0] = NOW + timedelta(seconds=delta)
        with pytest.raises(ValueError, match="stale_cessation"):
            s.recovery._finish(candidate, s.observed, NOW)
    s.clock[0] = NOW

    async def race(*args, **kwargs):
        with Session(s.engine) as db:
            db.add(PendingMessage(session_id=s.ids[0], seq=2, message_text="raced"))
            db.commit()
        return dict(s.observed)

    monkeypatch.setattr(EmberVmShimTransport, "get_session", race)
    with pytest.raises(ValueError):
        await s.recovery.apply(
            "operator", *s.ids, "reason", "stable", p["preview_sha256"]
        )


@pytest.mark.asyncio
async def test_learning_reconciliation_does_not_hide_other_unknowns_or_changed_history(
    learning_recovery_world,
):
    from factory.execution.models import AgentSession, AgentTurn
    from factory.orchestration import refill_control as api

    s = learning_recovery_world
    p = await s.recovery.preview(*s.ids, "reason")
    await s.recovery.apply("operator", *s.ids, "reason", "stable", p["preview_sha256"])
    with Session(s.engine) as db:
        other = AgentSession(
            local_session_id="another:qwen-drain:factory-retro-daily",
            workspace=".",
            branch="main",
            status="failed",
        )
        db.add(other)
        db.flush()
        db.add(
            AgentTurn(
                session_id=other.id,
                seq=1,
                prompt="",
                result_text="",
                terminal_reason="error",
                stop_reason="invocation_outcome_unknown",
            )
        )
        db.commit()
    with pytest.raises(ValueError, match="execution_unknown"):
        api.preview(True, "opus", True, 1, "activate")
    with Session(s.engine) as db:
        db.get(AgentSession, s.ids[0]).prior_cli_session_id = "changed-history"
        db.commit()
    with Session(s.engine) as db:
        assert not s.recovery.resolutions(db, {s.ids[0]})


@pytest.mark.asyncio
async def test_learning_reconciliation_concurrent_identical_apply_is_one_audit(
    learning_recovery_world,
):
    import asyncio

    s = learning_recovery_world
    p = await s.recovery.preview(*s.ids, "reason")
    args = ("operator", *s.ids, "reason", "stable", p["preview_sha256"])
    results = await asyncio.gather(s.recovery.apply(*args), s.recovery.apply(*args))
    assert results[0] == results[1]
    with Session(s.engine) as db:
        assert (
            len(
                db.exec(
                    select(FactoryAudit).where(FactoryAudit.action == s.recovery.ACTION)
                ).all()
            )
            == 1
        )


@pytest.mark.asyncio
async def test_learning_reconciliation_preserves_receipt_holds_and_other_session_fence(
    learning_recovery_world,
):
    from factory.execution import store
    from factory.execution.models import AgentSession, AgentTurn

    s = learning_recovery_world
    with Session(s.engine) as db:
        receipts = [
            FactoryReceipt(
                id=i,
                repo="jomcgi-org/homelab",
                issue_number=i,
                actor="owner",
                title="held",
                body="",
                url="",
                state="uncertain" if i == 996 else "escalated",
                task_paused=i != 996,
                allowance_json='{"spent":8}',
                direction_json='{"landing_hold":true}',
            )
            for i in (927, 996, 1027)
        ]
        other = AgentSession(
            local_session_id="factory:unrelated",
            workspace=".",
            branch="main",
            status="failed",
        )
        db.add_all([*receipts, other])
        db.flush()
        other_id = other.id
        db.add(
            AgentTurn(
                session_id=other_id,
                seq=1,
                prompt="",
                result_text="",
                terminal_reason="error",
                stop_reason="invocation_outcome_unknown",
            )
        )
        db.commit()
        before = [
            r.model_dump()
            for r in db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
        ]
    p = await s.recovery.preview(*s.ids, "reason")
    await s.recovery.apply("operator", *s.ids, "reason", "stable", p["preview_sha256"])
    with Session(s.engine) as db:
        assert [
            r.model_dump()
            for r in db.exec(select(FactoryReceipt).order_by(FactoryReceipt.id)).all()
        ] == before
        with pytest.raises(store.SessionOutcomeUnknown):
            store._assert_sendable(db, other_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity", [(True, 1, 1), (1, 2, 1), (1, 1, False), (0, 1, 1), (99999, 1, 1)]
)
async def test_learning_reconciliation_rejects_nonexact_identity(
    learning_recovery_world, identity
):
    s = learning_recovery_world
    with pytest.raises(ValueError):
        await s.recovery.preview(*identity, "reason")
    assert not s.calls


@pytest.fixture
def historical_learning_world(learning_recovery_world, monkeypatch):
    from copy import deepcopy

    from factory.execution.transport import EmberSessionGone, EmberVmShimTransport

    s = learning_recovery_world
    s.observed["state"] = "evicted"
    s.observed["historical_cessation"] = {
        "schema": 1,
        "source": "retained_control_plane_journal",
        "authority_scope": "single_writer_dispatch",
        "lineage_id": "old-guest",
        "node_id": "original-node",
        "vm_id": "original-vm",
        "evidence_sha256": "e" * 64,
        "observed_at_unix_ms": int(s.clock[0].timestamp() * 1000),
        "complete": True,
        "no_successor": True,
        "no_pending_restore": True,
    }
    s.historical_calls = []

    async def missing(*args, **kwargs):
        raise EmberSessionGone("projection pruned")

    async def evidence(self, guest_id, **identity):
        s.historical_calls.append((guest_id, identity))
        return deepcopy(s.observed)

    monkeypatch.setattr(EmberVmShimTransport, "get_session", missing)
    monkeypatch.setattr(
        EmberVmShimTransport, "get_session_cessation_evidence", evidence
    )
    return s


@pytest.mark.asyncio
async def test_historical_learning_proof_replays_concurrently_and_preserves_history(
    historical_learning_world,
    monkeypatch,
):
    import asyncio
    import json

    from factory.execution.transport import EmberVmShimTransport

    s = historical_learning_world
    before = _learning_snapshot(s)
    p = await s.recovery.preview(*s.ids, "exact historical cessation")
    assert s.historical_calls == [
        (
            "old-guest",
            {
                "generation": 0,
                "invoke_started_at": s.observed["invoke_started_at"],
                "updated_at": s.observed["updated_at"],
            },
        )
    ]
    s.clock[0] += timedelta(seconds=1)
    s.observed["historical_cessation"]["observed_at_unix_ms"] += 1000
    args = (
        "operator",
        *s.ids,
        "exact historical cessation",
        "stable-historical",
        p["preview_sha256"],
    )
    results = await asyncio.gather(s.recovery.apply(*args), s.recovery.apply(*args))
    assert results[0] == results[1]
    assert results[0]["unknown_outcome_preserved"] is True
    assert _learning_snapshot(s) == before
    with Session(s.engine) as db:
        rows = db.exec(
            select(FactoryAudit).where(FactoryAudit.action == s.recovery.ACTION)
        ).all()
        assert len(rows) == 1
        detail = json.loads(rows[0].detail_json)
        assert (
            detail["authority_observed_at_unix_ms"]
            == s.observed["historical_cessation"]["observed_at_unix_ms"]
        )
        assert s.recovery.resolutions(db, {s.ids[0]}) == {s.ids[0]}

    async def forbidden(*args, **kwargs):
        raise AssertionError("durable replay must precede remote I/O")

    monkeypatch.setattr(EmberVmShimTransport, "get_session", forbidden)
    assert await s.recovery.apply(*args) == results[0]
    with pytest.raises(ValueError, match="learning_request_conflict"):
        await s.recovery.apply(
            "operator", *s.ids, "changed", "stable-historical", p["preview_sha256"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("source", "saved_supervisor_observation"),
        ("authority_scope", "inventory_absence"),
        ("complete", False),
        ("no_successor", 1),
        ("no_pending_restore", False),
        ("lineage_id", ""),
        ("lineage_id", "x" * 129),
        ("node_id", ""),
        ("vm_id", None),
        ("evidence_sha256", "invalid"),
        ("observed_at_unix_ms", None),
        ("observed_at_unix_ms", True),
        ("observed_at_unix_ms", int((NOW - timedelta(seconds=31)).timestamp() * 1000)),
        ("observed_at_unix_ms", int((NOW + timedelta(seconds=1)).timestamp() * 1000)),
    ],
)
async def test_historical_learning_requires_complete_fresh_authority(
    historical_learning_world,
    field,
    value,
):
    s = historical_learning_world
    s.observed["historical_cessation"][field] = value
    with pytest.raises(ValueError, match="learning_historical_cessation_invalid"):
        await s.recovery.preview(*s.ids, "reason")
    with Session(s.engine) as db:
        assert not s.recovery.resolutions(db, {s.ids[0]})


@pytest.mark.asyncio
async def test_historical_learning_rejects_changed_journal_between_preview_apply(
    historical_learning_world,
):
    s = historical_learning_world
    p = await s.recovery.preview(*s.ids, "reason")
    s.observed["historical_cessation"]["evidence_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="learning_preview_changed"):
        await s.recovery.apply(
            "operator", *s.ids, "reason", "stable", p["preview_sha256"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["ember_lineage_id", "prior_ember_lineage_id", "ember_session_id"]
)
async def test_historical_learning_rechecks_local_lineage_successor(
    historical_learning_world,
    field,
):
    from factory.execution.models import AgentSession

    s = historical_learning_world
    p = await s.recovery.preview(*s.ids, "reason")
    with Session(s.engine) as db:
        other = AgentSession(
            local_session_id="successor",
            status="failed",
            model="luna",
            workspace=".",
            branch="main",
        )
        setattr(other, field, "old-guest")
        db.add(other)
        db.commit()
    expected = (
        "learning_delivery_or_pending_work"
        if field == "ember_session_id"
        else "learning_historical_lineage_owned"
    )
    with pytest.raises(ValueError, match=expected):
        await s.recovery.apply(
            "operator", *s.ids, "reason", "stable", p["preview_sha256"]
        )


@pytest.mark.asyncio
async def test_historical_learning_resolution_does_not_hide_later_lineage_owner(
    historical_learning_world,
):
    from factory.execution.models import AgentSession

    s = historical_learning_world
    p = await s.recovery.preview(*s.ids, "reason")
    await s.recovery.apply("operator", *s.ids, "reason", "stable", p["preview_sha256"])
    with Session(s.engine) as db:
        db.add(
            AgentSession(
                local_session_id="successor",
                status="running",
                ember_lineage_id="old-guest",
                model="luna",
                workspace=".",
                branch="main",
            )
        )
        db.commit()
        assert not s.recovery.resolutions(db, {s.ids[0]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [PermissionError("denied"), TimeoutError("timeout")]
)
async def test_learning_permission_or_timeout_does_not_select_historical_reader(
    historical_learning_world,
    monkeypatch,
    failure,
):
    from factory.execution.transport import EmberVmShimTransport

    s = historical_learning_world

    async def blocked(*args, **kwargs):
        raise failure

    monkeypatch.setattr(EmberVmShimTransport, "get_session", blocked)
    with pytest.raises(ValueError, match="learning_cessation_unavailable"):
        await s.recovery.preview(*s.ids, "reason")
    assert not s.historical_calls


@pytest.mark.asyncio
async def test_historical_learning_refusal_remains_auditable_and_fenced(
    historical_learning_world,
    monkeypatch,
):
    from factory.execution.transport import (
        EmberHistoricalCessationUnavailable,
        EmberVmShimTransport,
    )

    s = historical_learning_world
    before = _learning_snapshot(s)

    async def incomplete(*args, **kwargs):
        raise EmberHistoricalCessationUnavailable("history_compacted")

    monkeypatch.setattr(
        EmberVmShimTransport, "get_session_cessation_evidence", incomplete
    )
    with pytest.raises(
        ValueError, match="learning_historical_cessation_unavailable:history_compacted"
    ):
        await s.recovery.preview(*s.ids, "reason")
    assert _learning_snapshot(s) == before


@pytest.mark.asyncio
async def test_historical_learning_rejects_conflicting_original_lineage(
    historical_learning_world,
):
    from factory.execution.models import AgentSession

    s = historical_learning_world
    with Session(s.engine) as db:
        db.get(AgentSession, s.ids[0]).prior_ember_lineage_id = "different-lineage"
        db.commit()
    with pytest.raises(ValueError, match="learning_historical_lineage_changed"):
        await s.recovery.preview(*s.ids, "reason")


def test_routines_rotate_one_sentence_briefs(world):
    _engine, policy, clock = world
    seen = []
    for _ in range(len(refill.ROUTINES) + 1):
        start = refill.observe(policy, eligible=0, complete=True)
        assert start["ok"]
        with controls._read_session() as db:
            detail = refill._detail(
                refill.latest_event(db, "started", run_id=start["run_id"])
            )
            body = db.get(FactoryReceipt, start["receipt_id"]).body
        seen.append(detail["routine"])
        assert refill.ROUTINES[detail["routine"]] in body
        assert refill.READ_ONLY_BOUNDARY in body
        refill.settle(start["run_id"], success=True, new_issues=3, summary="ok")
        with Session(world[0]) as db:
            db.get(FactoryReceipt, start["receipt_id"]).state = "succeeded"
            db.commit()
        clock[0] += timedelta(minutes=normalize(policy)["cooldown_minutes"])
    assert seen == [*refill.ROUTINES, next(iter(refill.ROUTINES))]
    # Each routine is a single sentence.
    assert all(text.count(". ") == 0 for text in refill.ROUTINES.values())


@pytest.mark.parametrize(
    ("walled", "allowed", "expected"),
    [
        (set(), ["opus", "astra"], "astra"),
        ({"astra"}, ["opus", "astra"], "opus"),
        ({"astra", "opus"}, ["opus", "astra"], "astra"),
        ({"astra"}, ["astra"], "astra"),
    ],
)
def test_refill_review_falls_back_to_opus_while_astra_is_walled(
    world, monkeypatch, walled, allowed, expected
):
    import json

    _engine, policy, _clock = world

    def quota(model, *, force=True):
        assert force is False
        if model in walled:
            raise ValueError("refill_subscription_unavailable")
        return {"provider": model}

    monkeypatch.setattr(refill, "quota", quota)
    if allowed != policy["allowed_models"]:
        update(world, allowed_models=allowed)
        with Session(world[0]) as db:
            policy = json.loads(db.get(FactoryControl, "factory").policy_json)
    start = refill.observe(policy, eligible=0, complete=True)
    assert start["ok"]
    with controls._read_session() as db:
        detail = refill._detail(
            refill.latest_event(db, "started", run_id=start["run_id"])
        )
    assert detail["review_contract"]["model"] == expected
    assert refill.role(detail, refill.REVIEW_NODE) == {
        "model": expected,
        "effort": "xhigh",
    }
    with pytest.raises(ValueError, match="invalid refill review contract"):
        refill.role(
            {**detail, "review_contract": {**detail["review_contract"], "model": "x"}},
            refill.REVIEW_NODE,
        )


def test_refill_control_accepts_opus_review_evidence_unless_hourly(
    control_world, monkeypatch
):
    from factory.orchestration import refill_control as api

    _engine, _policy, clock = control_world

    def quota(model):
        if model == "astra":
            raise ValueError("refill_subscription_unavailable")
        return {"provider": "claude", "checked_at": clock[0].isoformat()}

    monkeypatch.setattr(api, "_quota", quota)
    p = api.preview(True, "opus", False, 0, "astra walled")
    assert p["new_run_review_contract"] == refill.REVIEW_FALLBACK
    assert set(p["subscription"]) == {"opus"}
    with pytest.raises(ValueError, match="refill_subscription_unavailable"):
        api.preview(True, "opus", True, 0, "hourly learning needs codex")
    # observe() requires Astra allowed, so the control must too.
    update(
        control_world,
        allowed_models=["opus"],
        backlog_refill={"enabled": False, "model": "opus"},
    )
    with pytest.raises(ValueError, match="refill_model_not_allowed"):
        api.preview(True, "opus", False, 0, "astra not allowed")


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
def test_inspection_commit_is_shared_and_does_not_follow_ref_movement(
    reviewed_run, monkeypatch
):
    from factory.orchestration import factory_conductor as conductor
    from factory.orchestration import node_workflows

    state = reviewed_run
    source = refill.inspection_source(state.start)
    assert source == {
        "version": 1,
        "repo": state.start["repo"],
        "ref": "main",
        "sha": "a" * 40,
    }
    for run in (state.collector, state.reviewer):
        pin = node_workflows._validate_pin(run["pin"])
        assert pin["branch"] == pin["hydration_branch"] == "commit:" + source["sha"]
        prompt = node_workflows._node_prompt(
            pin["prompt"],
            pin["artifact_path"],
            pin["artifact_schema"],
            branch=pin["branch"],
        )
        assert f"Inspect exactly commit {source['sha']}" in prompt
        assert f"Inspect branch factory/{state.task_id}" not in prompt
        assert pin["artifact_schema"]["x-factory-refill-inspection"] == source
    monkeypatch.setattr(
        conductor, "github_get", lambda *_: pytest.fail("pinned source read again")
    )
    assert refill.prepare_inspection(conductor._task(state.task_id), state.start)
    state.reconcile()
    assert refill.inspection_source(state.start) == source
    with controls._read_session() as db:
        assert len(refill._events(db, "inspection_pinned")) == 1


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize(
    "reviewed_run",
    ["empty", "blocked", "source_mismatch", "review_blocked"],
    indirect=True,
)
def test_inspection_outcomes_distinguish_completed_empty_from_blocked(
    reviewed_run, request
):
    from factory.orchestration import factory_conductor as conductor

    state = reviewed_run
    state.reconcile()
    scenario = request.node.callspec.params["reviewed_run"]
    expected = "succeeded" if scenario == "empty" else "failed"
    with Session(state.engine) as db:
        owner = db.get(FactoryReceipt, state.start["receipt_id"])
        assert owner.state == expected
        if scenario != "source_mismatch":
            completed = refill.latest_event(
                db, "completed", run_id=state.start["run_id"]
            )
            assert refill._detail(completed)["success"] == (scenario == "empty")
            assert refill._detail(completed)["new_issues"] == 0
    runs = conductor.graph.node_runs(state.task_id)
    assert len(runs) == (1 if scenario in {"blocked", "source_mismatch"} else 2)
    assert not state.github[1]
    before = [(r["id"], r["status"], r["cost_usd"]) for r in runs]
    state.reconcile()
    assert [
        (r["id"], r["status"], r["cost_usd"])
        for r in conductor.graph.node_runs(state.task_id)
    ] == before


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("failure", ["missing", "malformed", "changed_owner"])
def test_inspection_preparation_fails_before_any_model_start(
    world, monkeypatch, failure
):
    from factory.orchestration import backlog_refill_worker as worker
    from factory.orchestration import factory_conductor as conductor

    engine, policy, _clock = world
    for module in (conductor, conductor.graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    start = refill.observe(policy, eligible=0, complete=True)
    task_id = admit(world, start)
    task = conductor._task(task_id)

    def read(_repo, suffix):
        assert suffix == "commits/main"
        if failure == "missing":
            response = httpx.Response(
                404,
                request=httpx.Request(
                    "GET",
                    "https://api.github.com/repos/jomcgi-org/homelab/commits/main",
                ),
            )
            response.raise_for_status()
        if failure == "changed_owner":
            with Session(engine) as db:
                db.get(FactoryReceipt, start["receipt_id"]).task_paused = True
                db.commit()
            return {"sha": "a" * 40}
        return {"sha": "not-a-commit"}

    monkeypatch.setattr(conductor, "github_get", read)
    worker.reconcile(task, policy, [], [], 0)
    assert conductor.graph.node_runs(task_id) == []
    assert conductor.graph.load_graph(task_id) == []
    with Session(engine) as db:
        owner = db.get(FactoryReceipt, start["receipt_id"])
        assert owner.state == ("admitted" if failure == "changed_owner" else "failed")
        assert not refill._events(db, "inspection_pinned")
        assert db.exec(select(FactoryStart)).all() == []


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
def test_inspection_source_cannot_be_changed_by_artifact_or_dispatch_pin(reviewed_run):
    from factory.orchestration import node_workflows

    state = reviewed_run
    for field in ("branch", "hydration_branch", "repo"):
        pin = {**state.collector["pin"], field: "b" * 40}
        with pytest.raises(ValueError, match="inspection pin"):
            node_workflows._validate_pin(pin)
    with Session(state.engine) as db:
        row = db.get(SwarmNodeRun, state.collector["id"])
        import json

        pin = json.loads(row.pin_json)
        pin["branch"] = "b" * 40
        row.pin_json = dumps(pin)
        db.commit()
    with pytest.raises(ValueError, match="inspection pin|source mismatch"):
        writer.review(state.start["run_id"], state.reviewer["id"])
    assert not state.github[1]


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
def test_losing_preparation_failure_cannot_settle_pinned_run(world, monkeypatch):
    from factory.orchestration import backlog_refill_worker as worker
    from factory.orchestration import factory_conductor as conductor

    engine, policy, _clock = world
    for module in (conductor, conductor.graph):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    start = refill.observe(policy, eligible=0, complete=True)
    task_id = admit(world, start)

    def read(_repo, _suffix):
        with controls._locked_session() as (db, _control):
            refill.record(
                db,
                "inspection_pinned",
                run_id=start["run_id"],
                source={
                    "version": 1,
                    "repo": policy["repo"],
                    "ref": "main",
                    "sha": "a" * 40,
                },
            )
        httpx.Response(
            404, request=httpx.Request("GET", "https://api.github.com")
        ).raise_for_status()

    monkeypatch.setattr(conductor, "github_get", read)
    worker.reconcile(conductor._task(task_id), policy, [], [], 0)
    with Session(engine) as db:
        assert db.get(FactoryReceipt, start["receipt_id"]).state == "admitted"
        assert len(refill._events(db, "inspection_pinned")) == 1
        assert not refill._events(db, "completed")
        assert db.exec(select(FactoryStart)).all() == []


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
def test_concurrent_source_preparation_keeps_first_pinned_commit(world, monkeypatch):
    from threading import Barrier, Lock
    from factory.orchestration import factory_conductor as conductor

    engine, policy, _clock = world
    monkeypatch.setattr(conductor, "get_engine", lambda: engine)
    start = refill.observe(policy, eligible=0, complete=True)
    task_id = admit(world, start)
    task = conductor._task(task_id)
    with controls._read_session() as db:
        detail = refill._detail(
            refill.latest_event(db, "started", run_id=start["run_id"])
        )
    barrier, lock, counter = Barrier(2), Lock(), []

    def read(_repo, _suffix):
        with lock:
            counter.append(len(counter))
            sha = ("a" if len(counter) == 1 else "b") * 40
        barrier.wait(timeout=5)
        return {"sha": sha}

    monkeypatch.setattr(conductor, "github_get", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: refill.prepare_inspection(task, detail), range(2))
        )
    assert results == [True, True]
    with controls._read_session() as db:
        assert len(refill._events(db, "inspection_pinned")) == 1
        assert db.exec(select(FactoryStart)).all() == []
    assert refill.inspection_source(detail)["sha"] in {"a" * 40, "b" * 40}


@pytest.fixture
def preflight_run(reviewed_run, monkeypatch, request):
    """Persist an authenticated guest preflight error through the real writer."""
    import hashlib
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from factory.execution import admission, store, result_receipts
    from factory.execution.constants import exact_dispatch_id
    from factory.execution.models import (
        AgentSession,
        AgentTurn,
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentResultReceipt,
        PendingMessage,
    )
    from factory.execution import transport
    from factory.execution.transport import parse_native_turn
    from factory.orchestration import factory_conductor as conductor, node_workflows

    s = reviewed_run
    SQLModel.metadata.create_all(
        s.engine,
        tables=[
            m.__table__
            for m in (
                AgentTurn,
                AgentCapacityPool,
                AgentCapacityReservation,
                AgentResultReceipt,
                PendingMessage,
            )
        ],
    )
    import core.db

    for module in (admission, store, result_receipts, core.db):
        monkeypatch.setattr(module, "get_engine", lambda: s.engine)
    pin = s.collector["pin"]
    with Session(s.engine) as db:
        agent = AgentSession(
            **node_workflows._expected_session_identity(pin),
            workspace="guest",
            admission_tier="project",
            ember_session_id="preflight-guest",
        )
        db.add(agent)
        db.flush()
        s.sid = agent.id
        db.add(
            PendingMessage(
                session_id=s.sid, seq=1, message_text="inspect", model="opus"
            )
        )
        db.commit()
    owner = "preflight-executor"
    assert store.claim_pending_message_for_session_sync(s.sid, owner) == 1
    assert admission.recheck(s.sid, 1, owner, "claude-runtime")
    dispatch = exact_dispatch_id(s.sid, "preflight-guest", 1, owner, 1)
    s.native = {
        "result": "Pinned inspection checkout unavailable before model execution",
        "terminal_reason": "error",
        "stop_reason": "refill_inspection_preflight_failed",
        "is_error": True,
        "num_turns": 0,
        "activities": [],
        "permission_denials": [],
        "dispatch_id": dispatch,
        "turn_seq": 1,
        "usage": {
            "refill_inspection_preflight": {
                "version": 1,
                "expected_sha": "a" * 40,
                "observed_sha": None,
                "tracked_clean": False,
                "invocation_started": False,
                "dispatch_id": dispatch,
                "turn_seq": 1,
            }
        },
    }
    envelope = result_receipts.prepare_receipt(
        s.sid, owner, 1, "preflight-guest", b"request"
    )
    s.receipt_id = envelope["id"]
    with Session(s.engine) as db:
        receipt = db.get(AgentResultReceipt, s.receipt_id)
        body = dumps(s.native).encode()
        receipt.result_body = body
        receipt.result_sha256 = hashlib.sha256(body).hexdigest()
        receipt.received_at = datetime.now(timezone.utc)
        db.add(receipt)
        db.commit()
    import asyncio

    ordering = getattr(request, "param", "callback")

    async def observe():
        post_release, observed = asyncio.Event(), asyncio.Event()

        async def database_call(fn, *args, **kwargs):
            if fn is result_receipts.read_active_result and ordering == "http":
                await observed.wait()
            value = fn(*args, **kwargs)
            if fn is result_receipts.mark_response_observed:
                observed.set()
            return value

        monkeypatch.setattr(transport, "_receipt_database_call", database_call)

        async def post():
            if ordering != "http":
                await post_release.wait()
            if ordering == "lost":
                raise OSError("original response lost after callback committed")
            return parse_native_turn(s.native, "preflight-guest")

        result = await transport._observe_native_result(
            post,
            {
                "receipt_id": s.receipt_id,
                "session_id": s.sid,
                "claim_owner": owner,
                "dispatch_count": 1,
                "guest_id": "preflight-guest",
                "request_sha256": hashlib.sha256(b"request").hexdigest(),
            },
            None,
            None,
            5,
        )
        post_release.set()
        await asyncio.gather(
            *list(transport._receipt_observers), return_exceptions=True
        )
        assert (result.native_receipt is None) == (ordering == "http")
        return result

    turn = asyncio.run(observe())
    store.persist_turn_from_pending_sync(
        s.sid,
        1,
        "inspect",
        turn,
        s.native["result"],
        store.turn_status(turn),
        claim_owner=owner,
        dispatch_count=1,
    )
    if ordering == "lost":
        # The ordinary execution owner releases a receipt winner in its finally
        # before cleanup. Recovery must refuse until that fence actually clears.
        from factory.execution.api import read_refill_preflight_factory_attempt

        with Session(s.engine) as db:
            assert (
                read_refill_preflight_factory_attempt(db, pin, s.sid, "SUCCESS") is None
            )
        assert result_receipts.release_unobserved_fence(
            s.receipt_id, s.sid, owner, 1, "preflight-guest"
        )
        with Session(s.engine) as db:
            assert store.clear_ember_bindings_by_ember_id(db, "preflight-guest") == [
                s.sid
            ]
    assert conductor.graph.record_dispatch(s.task_id, refill.NODE, 1, s.sid, None).ok
    s.result = {
        "status": "uncertain",
        "session_id": s.sid,
        "attempt": 1,
        "cost_usd": None,
        "head_sha": None,
        "artifact": None,
        "value": None,
        "reason": "terminal reason does not confirm clean completion; reconcile before retry",
        "cost_basis": "unknown",
        "accounting": "reserved_cost",
    }
    s.dbos = SimpleNamespace(
        get_workflow_status=lambda _: SimpleNamespace(status="SUCCESS"),
        retrieve_workflow=lambda _: SimpleNamespace(get_result=lambda: s.result),
    )
    return s


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("reviewed_run", ["preflight"], indirect=True)
@pytest.mark.parametrize("preflight_run", ["http", "callback", "lost"], indirect=True)
def test_authenticated_refill_preflight_settles_failure_without_refunding_unknown_spend(
    preflight_run,
):
    import json
    from factory.execution.models import AgentTurn, AgentCapacityReservation
    from factory.orchestration import factory_conductor as conductor

    s = preflight_run
    conductor.reconcile_task(s.task_id, s.policy, s.dbos)
    [run] = conductor.graph.node_runs(s.task_id)
    assert run["status"] == "failed"
    outcome = json.loads(run["outcome_json"])
    assert outcome["invocation_phase"] == "refill_inspection_preflight_failed"
    assert outcome["cost_usd"] is None and outcome["cost_basis"] == "unknown"
    snapshot = controls.task_snapshot(s.task_id)
    assert snapshot["unresolved_starts"] == 0
    assert snapshot["starts"][0]["status"] == "failed"
    assert snapshot["committed_cost_usd"] > 0  # consumed reservation, never free work
    with Session(s.engine) as db:
        permit = db.exec(select(AgentCapacityReservation)).one()
        assert permit.state == "settled"
        assert permit.outcome == "error"  # preserve the native permit settlement
        turn = db.exec(select(AgentTurn)).one()
        assert turn.terminal_reason == "error" and turn.cost_usd is None
        assert (
            json.loads(turn.usage_json)["refill_inspection_preflight"]
            == s.native["usage"]["refill_inspection_preflight"]
        )
    # Reconciliation replay cannot consume another attempt or fabricate an audit.
    conductor.reconcile_task(s.task_id, s.policy, s.dbos)
    assert len(conductor.graph.node_runs(s.task_id)) == 1
    assert not s.github[1]


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("reviewed_run", ["preflight"], indirect=True)
@pytest.mark.parametrize(
    "case",
    [
        "missing_receipt",
        "digest",
        "unobserved",
        "new_dispatch",
        "wrong_guest",
        "wrong_source",
        "wrong_dispatch",
        "invoked",
        "recovery",
        "activity",
        "fence",
        "pending",
        "missing_contract",
        "changed_pin",
        "usage_forgery",
        "unknown",
    ],
)
def test_refill_preflight_refuses_incomplete_or_changed_proof(preflight_run, case):
    import json
    import hashlib
    from factory.execution.models import (
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )
    from factory.execution.api import read_refill_preflight_factory_attempt
    from factory.execution import result_receipts

    s = preflight_run
    pin = dict(s.collector["pin"])
    with Session(s.engine) as db:
        receipt = db.get(AgentResultReceipt, s.receipt_id)
        agent = db.get(AgentSession, s.sid)
        turn = db.exec(select(AgentTurn)).one()
        usage = json.loads(turn.usage_json)
        if case == "missing_receipt":
            db.delete(receipt)
        elif case == "digest":
            receipt.result_sha256 = "b" * 64
        elif case == "unobserved":
            receipt.response_observed_at = None
            receipt.response_observer_released_at = None
        elif case == "new_dispatch":
            receipt.dispatch_count = 2
        elif case == "wrong_guest":
            agent.ember_session_id = "successor"
        elif case in {"wrong_source", "wrong_dispatch", "invoked"}:
            field, value = {
                "wrong_source": ("expected_sha", "b" * 40),
                "wrong_dispatch": ("dispatch_id", "b" * 64),
                "invoked": ("invocation_started", True),
            }[case]
            s.native["usage"]["refill_inspection_preflight"][field] = value
            receipt.result_body = dumps(s.native).encode()
            receipt.result_sha256 = hashlib.sha256(receipt.result_body).hexdigest()
            db.flush()
            usage["native_result_receipt"] = result_receipts._result(
                db, result_receipts._receipt_metadata(db, s.receipt_id)
            )["provenance"]
            usage["refill_inspection_preflight"] = s.native["usage"][
                "refill_inspection_preflight"
            ]
        elif case == "recovery":
            usage["recovery"] = {}
        elif case == "activity":
            usage["activities"] = ["ran a tool"]
        elif case == "fence":
            agent.result_receipt_fence_id = s.receipt_id
        elif case == "pending":
            db.add(PendingMessage(session_id=s.sid, seq=2, message_text="new work"))
        elif case == "missing_contract":
            event = refill.latest_event(db, "started", run_id=s.start["run_id"])
            detail = json.loads(event.detail_json)
            detail.pop("inspection_contract")
            event.detail_json = dumps(detail)
        elif case == "changed_pin":
            pin["artifact_schema"] = {}
        elif case == "usage_forgery":
            usage["native_result_receipt"] = {}
        elif case == "unknown":
            from factory.execution.constants import UNKNOWN_INVOCATION

            turn.stop_reason = UNKNOWN_INVOCATION
        turn.usage_json = dumps(usage)
        db.commit()
        assert read_refill_preflight_factory_attempt(db, pin, s.sid, "SUCCESS") is None


@pytest.mark.parametrize("world", ["inspection"], indirect=True)
@pytest.mark.parametrize("reviewed_run", ["preflight"], indirect=True)
def test_refill_preflight_settlement_rechecks_receipt_after_read(preflight_run):
    from factory.execution.models import AgentResultReceipt, AgentCapacityReservation
    from factory.execution.api import (
        read_refill_preflight_factory_attempt,
        settle_refill_preflight_factory_attempt,
    )

    s = preflight_run
    with Session(s.engine) as db:
        proof = read_refill_preflight_factory_attempt(
            db, s.collector["pin"], s.sid, "SUCCESS"
        )
        assert proof is not None
        db.get(AgentResultReceipt, s.receipt_id).superseded_at = NOW
        db.flush()
        with pytest.raises(ValueError, match="factory_attempt_changed"):
            settle_refill_preflight_factory_attempt(db, s.collector["pin"], proof)
        assert db.exec(select(AgentCapacityReservation)).one().outcome == "error"
