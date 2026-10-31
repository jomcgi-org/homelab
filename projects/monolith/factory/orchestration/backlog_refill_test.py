"""Default-off refill, durable starts, recurrence and publication with fake I/O."""

from __future__ import annotations

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
def world(tmp_path, monkeypatch):
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
    clock = [NOW]
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(controls, "_now", lambda: clock[0])
    monkeypatch.setattr(intake, "_now", lambda: clock[0])
    monkeypatch.setattr(
        refill,
        "quota",
        lambda model: {"model": model, "checked_at": clock[0].isoformat()},
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
        conductor, "github_get", lambda *_: {"object": {"sha": "a" * 40}}
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
    monkeypatch.setattr(refill, "quota", lambda model: checked.append(model) or {})

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
    settle(collector, collection)
    reconcile()
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
    with pytest.raises(ValueError, match="refill_in_flight"):
        api.preview(True, "astra", False, 0, "re-arm", reset_breaker=True)


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
