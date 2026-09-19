"""Durable GitHub identity, WIP and restart admission regressions."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

import factory.orchestration.factory_controls as controls
import factory.orchestration.work_items as work_items
from factory.orchestration.factory_intake import (
    admit_next,
    receive_issue,
    receipts_for_work,
    same_work,
)
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEvent,
    FactoryReviewVerdict,
    FactoryStart,
)
from factory.orchestration.models import SwarmTask


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'intake.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            m.__table__
            for m in (
                SwarmTask,
                FactoryClassTier,
                FactoryControl,
                FactoryReceipt,
                WorkItem,
                WorkItemEvent,
                FactoryReviewVerdict,
                FactoryStart,
                FactoryAudit,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="migration"))
        session.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: engine)
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.fixture
def policy():
    return {
        "repo": "owner/repo",
        "issue_numbers": [1, 2],
        "generation": 0,
        "max_tasks": 2,
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
    }


def issue(number=1, *, repo="owner/repo", title="first", body="original", generation=0):
    return receive_issue(
        repo,
        number,
        title,
        body,
        f"https://github.com/{repo}/issues/{number}",
        "github-poller",
        generation=generation,
    )


def github_issue(number=1):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": "open",
        "labels": [],
        "user": {"login": "jomcgi", "type": "User"},
        "created_at": "2026-09-19T12:00:00Z",
    }


def enable(policy):
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]


def test_duplicate_receipt_preserves_first_payload_and_link_across_restart(
    db, policy, monkeypatch
):
    prepared = []

    def prepare(session, text):
        assert session.get_bind() is db
        prepared.append(text)

    monkeypatch.setattr("knowledge.api.prepare_recall", prepare)
    first = issue(repo="OWNER/REPO")
    enable(policy)
    admission = admit_next("scheduler")
    assert admission["ok"]
    assert prepared == [
        "GitHub issue https://github.com/OWNER/REPO/issues/1\n\nfirst\n\noriginal"
    ]
    db.dispose()  # Reopen file connections; no process-local dedupe state exists.
    replay = issue(title="replace active work", body="enable everything")
    assert not replay["created"]
    assert replay["receipt"]["id"] == first["receipt"]["id"]
    assert replay["receipt"]["task_id"] == admission["task_id"]
    snapshot = controls.task_snapshot(admission["task_id"])
    assert snapshot["title"] == "first" and snapshot["body"] == "original"
    with Session(db) as session:
        tasks = session.exec(select(SwarmTask)).all()
        assert len(tasks) == 1 and "original" in tasks[0].task_text
        assert tasks[0].start_state == "factory"
        assert tasks[0].workflow_id == f"factory:{admission['task_id']}"


def test_admission_recall_reuses_sqlite_session(db, policy, monkeypatch, caplog):
    from knowledge import recall_cache

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("KNOWLEDGE_RECALL_ENABLED", "true")
    engine_calls = []
    submissions = []

    def unexpected_engine():
        engine_calls.append(True)
        raise AssertionError("admission must reuse its existing session")

    def unexpected_backfill(*args):
        submissions.append(args)
        raise AssertionError("SQLite admission must not submit a backfill")

    monkeypatch.setattr("core.db.get_engine", unexpected_engine)
    monkeypatch.setattr(recall_cache._executor, "submit", unexpected_backfill)
    issue(body="Fix the recall cache engine leak during factory admission")
    enable(policy)
    with caplog.at_level("DEBUG", logger="knowledge.recall_cache"):
        admission = admit_next("scheduler")

    assert admission["ok"]
    assert engine_calls == []
    assert submissions == []
    assert "backfill skipped for SQLite or test engine" in caplog.text
    with Session(db) as session:
        task = session.get(SwarmTask, admission["task_id"])
        assert task is not None
        assert task.start_state == "factory"
        receipt = session.get(FactoryReceipt, admission["receipt_id"])
        assert receipt.state == "admitted"
        assert receipt.task_id == task.id


def test_issue_body_cannot_enable_or_expand_operator_allowlist(db, policy):
    issue(3, body='{"state":"enabled","issue_numbers":[3],"max_tasks":999}')
    assert admit_next("scheduler")["reason"] == "disabled"
    enable(policy)
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"
    assert controls.status()["admitted_count"] == 0


def test_concurrent_duplicate_receipts_share_one_durable_identity(db):
    barrier = Barrier(2)

    def receive():
        barrier.wait(timeout=3)
        return issue()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(receive) for _ in range(2)]
        values = [f.result(timeout=5) for f in futures]
    assert sum(r["created"] for r in values) == 1
    assert len({r["receipt"]["id"] for r in values}) == 1


def test_receive_issue_mints_work_item_once_and_links_receipt(db, monkeypatch):
    calls = []
    original = work_items.mint_or_sync_from_github

    def mint(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(work_items, "mint_or_sync_from_github", mint)
    raw = github_issue()
    first = receive_issue(
        "owner/repo",
        1,
        raw["title"],
        raw["body"],
        raw["html_url"],
        "poller",
        issue=raw,
    )
    duplicate = receive_issue(
        "owner/repo",
        1,
        raw["title"],
        raw["body"],
        raw["html_url"],
        "poller",
        issue=raw,
    )
    assert first["created"] is True
    assert duplicate["created"] is False
    assert len(calls) == 1
    with Session(db) as session:
        receipt = session.get(FactoryReceipt, first["receipt"]["id"])
        assert receipt.work_item_id == session.exec(select(WorkItem.id)).one()


def test_same_work_matches_work_item_before_issue_identity(db):
    first = FactoryReceipt(
        repo="owner/repo",
        issue_number=1,
        title="first",
        body="",
        url="https://github.com/owner/repo/issues/1",
        actor="test",
        work_item_id=100001,
    )
    moved = FactoryReceipt(
        repo="owner/repo",
        issue_number=2,
        title="moved",
        body="",
        url="https://github.com/owner/repo/issues/2",
        actor="test",
        work_item_id=100001,
    )
    assert same_work(first, moved)
    with Session(db) as session:
        session.add(
            WorkItem(
                id=100001,
                title="test item",
                state="open",
                source_kind="github",
                trust="trusted",
                github_repo="owner/repo",
                github_issue_number=1,
            )
        )
        session.commit()
        session.add(first)
        session.add(moved)
        session.commit()

        rows = receipts_for_work(session, "owner/repo", 1)
        assert any(r.issue_number == 1 for r in rows)

        rows = receipts_for_work(session, "owner/repo", 2)
        assert any(r.issue_number == 2 for r in rows)


def test_active_receipt_blocks_admission_of_shared_work_item(db):
    with Session(db) as session:
        work_item = WorkItem(
            id=100001,
            title="shared",
            state="open",
            source_kind="github",
            trust="trusted",
            github_repo="owner/repo",
            github_issue_number=7,
        )
        session.add(work_item)
        session.commit()
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=7,
                title="first",
                body="",
                url="https://github.com/owner/repo/issues/7",
                actor="test",
                state="admitted",
                work_item_id=100001,
            )
        )
        session.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=9,
                title="second",
                body="",
                url="https://github.com/owner/repo/issues/9",
                actor="test",
                state="queued",
                work_item_id=100001,
            )
        )
        session.commit()

        rows = receipts_for_work(session, "owner/repo", 7)
        assert any(r.state == "admitted" for r in rows)

        rows = receipts_for_work(session, "owner/repo", 9)
        assert any(r.state == "queued" for r in rows)

        rows = receipts_for_work(session, "owner/repo", 9)
        rows_with_work_item = [r for r in rows if r.work_item_id == 100001]
        assert len(rows_with_work_item) == 1


def test_concurrent_admissions_cannot_both_claim_wip(db, policy):
    enable(policy)
    issue(1)
    issue(2)
    barrier = Barrier(2)

    def admit():
        barrier.wait(timeout=3)
        return admit_next("scheduler")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(admit) for _ in range(2)]
        values = [f.result(timeout=5) for f in futures]
    assert sum(v["ok"] for v in values) == 1
    assert next(v for v in values if not v["ok"])["reason"] == "wip_limit"
    snapshot = controls.status()
    assert snapshot["admitted_count"] == 1 and len(snapshot["active_tasks"]) == 1
    with Session(db) as session:
        assert len(session.exec(select(SwarmTask)).all()) == 1


def test_max_tasks_bounds_tasks_in_flight_not_tasks_ever_admitted(db, policy):
    policy["max_tasks"] = 1
    enable(policy)
    issue(1)
    issue(2)
    first = admit_next("scheduler")
    refused = admit_next("scheduler")
    assert refused["reason"] == "wip_limit" and refused["limit"] == 1
    assert controls.finish_task(first["task_id"], "failed", "scheduler")["ok"]
    second = admit_next("scheduler")
    assert second["ok"] and second["task_id"] != first["task_id"]
    assert controls.status()["admitted_count"] == 2


def test_concurrency_is_the_smaller_of_policy_and_chart_cap(db, policy, monkeypatch):
    monkeypatch.delenv("FACTORY_MAX_CONCURRENT_TASKS", raising=False)
    policy["max_tasks"] = 3
    enable(policy)
    for number in (1, 2, 3):
        issue(number)
    first = admit_next("scheduler")
    assert first["ok"]
    capped = admit_next("scheduler")
    assert capped["reason"] == "wip_limit" and capped["limit"] == 1
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    second = admit_next("scheduler")
    assert second["ok"] and second["task_id"] != first["task_id"]
    third = admit_next("scheduler")
    assert third["reason"] == "wip_limit" and third["active"] == 2
    assert third["active_task_ids"] == [first["task_id"], second["task_id"]]
    assert len(controls.status()["active_tasks"]) == 2


def test_recurrence_needs_distinct_explicit_operator_generation(db, policy):
    enable(policy)
    issue()
    first = admit_next("scheduler")
    controls.finish_task(first["task_id"], "failed", "scheduler")
    assert not issue()["created"]
    assert issue(generation=1)["created"]
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"
    policy["generation"] = 1
    enable(policy)
    second = admit_next("scheduler")
    assert second["ok"] and second["task_id"] != first["task_id"]
    assert controls.status()["admitted_count"] == 2


def test_receipt_and_admission_are_rollbackable_in_supplied_session(db, policy):
    enable(policy)
    with Session(db) as session:
        receive_issue(
            "owner/repo",
            1,
            "title",
            "body",
            "https://github.com/owner/repo/issues/1",
            "poller",
            session=session,
        )
        assert admit_next("scheduler", session=session)["ok"]
        session.rollback()
    snapshot = controls.status()
    assert snapshot["receipts"] == [] and snapshot["admitted_count"] == 0
    with Session(db) as session:
        assert session.exec(select(SwarmTask)).first() is None


def test_receipt_from_other_repo_cannot_be_admitted(db, policy):
    enable(policy)
    issue(repo="other/repo")
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"number": True},
        {"number": 0},
        {"number": -1},
        {"generation": True},
        {"generation": -1},
        {"repo": "../repo"},
        {"title": ""},
        {"title": "a" * 513},
        {"body": "a" * 65537},
    ],
)
def test_invalid_receipt_does_not_create_task_or_receipt(db, kwargs):
    with pytest.raises(ValueError):
        issue(**kwargs)
    assert controls.status()["receipts"] == []


def test_url_must_identify_the_same_issue(db):
    with pytest.raises(ValueError):
        receive_issue(
            "owner/repo",
            1,
            "title",
            "body",
            "https://github.com/owner/repo/issues/2",
            "poller",
        )
    assert controls.status()["receipts"] == []


def test_receive_issue_defaults_stores_and_validates_task_class(db):
    assert issue()["receipt"]["task_class"] == "bug-fix"
    assert issue(2, generation=1)["receipt"]["task_class"] == "bug-fix"
    refined = receive_issue(
        "owner/repo",
        3,
        "title",
        "body",
        "https://github.com/owner/repo/issues/3",
        "poller",
        task_class="refine",
    )
    assert refined["receipt"]["task_class"] == "refine"
    with Session(db) as session:
        audit = session.exec(
            select(FactoryAudit)
            .where(FactoryAudit.action == "receive_issue")
            .order_by(FactoryAudit.id.desc())
        ).first()
    detail = json.loads(audit.detail_json)
    assert detail["task_class"] == "refine" and "kind" not in detail
    with pytest.raises(ValueError, match="invalid task_class"):
        receive_issue(
            "owner/repo",
            4,
            "title",
            "body",
            "https://github.com/owner/repo/issues/4",
            "poller",
            task_class="other",
        )


def test_duplicate_receipt_of_one_class_cannot_replace_its_text(db):
    first = receive_issue(
        "owner/repo",
        5,
        "title",
        "body",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="refine",
    )
    replay = receive_issue(
        "owner/repo",
        5,
        "changed",
        "changed",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="refine",
    )
    assert replay["created"] is False
    assert replay["receipt"]["id"] == first["receipt"]["id"]
    assert replay["receipt"]["title"] == "title"


def test_a_second_class_is_a_second_receipt_in_the_same_generation(db):
    """A refine pass makes an issue ready, so the lane may then deliver it.

    Receipt identity carries the class precisely so this does not need an
    operator to bump the generation.
    """
    refined = receive_issue(
        "owner/repo",
        5,
        "title",
        "body",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="refine",
    )
    delivery = receive_issue(
        "owner/repo",
        5,
        "title",
        "body",
        "https://github.com/owner/repo/issues/5",
        "poller",
        task_class="bug-fix",
    )
    assert delivery["created"] is True
    assert delivery["receipt"]["id"] != refined["receipt"]["id"]
    assert delivery["receipt"]["task_class"] == "bug-fix"


def test_intake_actor_receipt_requires_enabled_intake(db, policy):
    policy["issue_numbers"] = [1]
    receive_issue(
        "owner/repo",
        9,
        "title",
        "body",
        "https://github.com/owner/repo/issues/9",
        "factory:intake",
    )
    enable(policy)
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"
    policy["intake"] = {"enabled": True}
    enable(policy)
    assert admit_next("scheduler")["ok"]


@pytest.mark.parametrize("intake_enabled", [False, True])
def test_allowlisted_receipt_is_admitted_regardless_of_intake(
    db, policy, intake_enabled
):
    policy["intake"] = {"enabled": intake_enabled}
    issue(1)
    enable(policy)
    assert admit_next("scheduler")["ok"]


def advisory_issue(number, *, repo="owner/repo", generation=0):
    return receive_issue(
        repo,
        number,
        f"advisory {number}",
        "body",
        f"https://github.com/{repo}/issues/{number}",
        "factory:intake",
        generation=generation,
        task_class="refine",
    )


def test_a_full_delivery_lane_does_not_block_an_advisory_admission(
    db, policy, monkeypatch
):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    policy["max_tasks"] = {"delivery": 1, "advisory": 1}
    policy["intake"] = {"enabled": True}
    enable(policy)
    issue(1)
    issue(2)
    advisory_issue(3)
    first = admit_next("scheduler")
    assert first["ok"] and first["lane"] == "delivery"
    second = admit_next("scheduler")
    assert second["ok"] and second["lane"] == "advisory"
    third = admit_next("scheduler")
    assert third["reason"] == "wip_limit"
    assert third["lanes"] == {
        "delivery": {"limit": 1, "active": 1},
        "advisory": {"limit": 1, "active": 1},
    }


def test_a_shut_advisory_lane_never_admits_advisory_work(db, policy, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    policy["max_tasks"] = 2
    policy["intake"] = {"enabled": True}
    enable(policy)
    advisory_issue(3)
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"


def test_the_caller_can_hold_one_lane_shut(db, policy, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "3")
    policy["max_tasks"] = {"delivery": 2, "advisory": 1}
    policy["intake"] = {"enabled": True}
    enable(policy)
    issue(1)
    advisory_issue(3)
    held = admit_next("scheduler", lanes=("advisory",))
    assert held["ok"] and held["lane"] == "advisory"
    assert admit_next("scheduler", lanes=("advisory",))["reason"] == "wip_limit"
    opened = admit_next("scheduler")
    assert opened["ok"] and opened["lane"] == "delivery"


def test_no_lane_at_all_admits_nothing_and_names_no_task(db, policy, monkeypatch):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    policy["max_tasks"] = {"delivery": 1, "advisory": 1}
    enable(policy)
    issue(1)
    refused = admit_next("scheduler", lanes=())
    assert refused["reason"] == "wip_limit"
    assert refused["task_id"] is None and refused["limit"] == 0


def test_a_receipt_written_before_classes_reads_as_delivery():
    """The column is NOT NULL now, but rows predating it read as bug-fix."""
    from factory.orchestration.factory_intake import lane_of
    from types import SimpleNamespace

    assert lane_of(SimpleNamespace(task_class=None)) == "delivery"
    assert lane_of(SimpleNamespace(task_class="refine")) == "advisory"


def rows(*lanes):
    """Receipts standing in for held slots, one per named lane."""
    from types import SimpleNamespace

    return [
        SimpleNamespace(task_class="bug-fix" if lane == "delivery" else "refine")
        for lane in lanes
    ]


def test_a_ceiling_below_the_lanes_never_starves_a_lane(db, policy, monkeypatch):
    """The live defect: at a ceiling of 4 with lanes 4 and 8, advisory got 0.

    Delivery took its whole maximum first, so a sweep excluded 272 advisory
    candidates as lane_full while eight advisory slots sat unusable.
    """
    from factory.orchestration.factory_intake import ceiling_below_lanes, open_lanes

    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    policy["max_tasks"] = {"delivery": 4, "advisory": 8}
    room = open_lanes(policy, [])
    assert 1 <= room["delivery"] <= 4
    assert room["advisory"] >= 1
    assert room["delivery"] + room["advisory"] == 4
    assert ceiling_below_lanes(policy) == {
        "ceiling": 4,
        "delivery_max": 4,
        "advisory_max": 8,
        "lanes_sum": 12,
    }


def test_the_contended_split_follows_what_each_lane_holds(db, policy, monkeypatch):
    from factory.orchestration.factory_intake import open_lanes

    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    policy["max_tasks"] = {"delivery": 4, "advisory": 8}
    # Advisory holding three of the four means the free slot goes to delivery,
    # which keeps its guaranteed slot under any ceiling.
    assert open_lanes(policy, rows("advisory", "advisory", "advisory")) == {
        "delivery": 1,
        "advisory": 0,
    }
    # A full ceiling is a full ceiling, whichever lane filled it.
    assert open_lanes(policy, rows(*["delivery"] * 4)) == {
        "delivery": 0,
        "advisory": 0,
    }


def test_a_ceiling_that_covers_both_lanes_leaves_them_uncontended(
    db, policy, monkeypatch
):
    from factory.orchestration.factory_intake import ceiling_below_lanes, open_lanes

    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "12")
    policy["max_tasks"] = {"delivery": 4, "advisory": 8}
    assert ceiling_below_lanes(policy) is None
    assert open_lanes(policy, []) == {"delivery": 4, "advisory": 8}
    assert open_lanes(policy, rows("delivery", "advisory")) == {
        "delivery": 3,
        "advisory": 7,
    }
    # A lane the caller holds shut offers no room and frees none to the other.
    assert open_lanes(policy, [], ("advisory",)) == {"delivery": 0, "advisory": 8}


def test_a_contended_ceiling_still_admits_advisory_work(db, policy, monkeypatch):
    """The split has to reach the real gate, not only the sweep's view of it.

    admit_next takes the oldest queued receipt across the lanes that have room,
    so what the old limits did was leave advisory with no room to be oldest
    into. It is admissible now, and delivery still keeps its guaranteed slot.
    """
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "4")
    policy["max_tasks"] = {"delivery": 4, "advisory": 8}
    policy["intake"] = {"enabled": True}
    policy["issue_numbers"] = [1, 2, 3, 4]
    enable(policy)
    advisory_issue(5)
    for number in (1, 2, 3, 4):
        issue(number)
    first = admit_next("scheduler")
    assert first["ok"] and first["lane"] == "advisory"
    assert admit_next("scheduler")["lane"] == "delivery"


def test_reconfigure_admits_new_policy_without_repinning_running_work(
    db, policy, monkeypatch
):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    enable(policy)
    issue(1)
    issue(2)
    first = admit_next("scheduler")
    before = controls.task_snapshot(first["task_id"])
    changed = {**policy, "generation": 1, "task_budget_usd": 10}
    assert controls.set_control("configure", "operator", policy=changed)["ok"]
    # The old queued receipt is inert. A new one on the active issue also
    # waits, but must not hide later eligible work in the same queue.
    assert admit_next("scheduler")["reason"] == "no_eligible_issue"
    issue(1, generation=1)
    issue(2, generation=1)
    second = admit_next("scheduler")
    assert second["ok"]
    new = controls.task_snapshot(second["task_id"])
    assert new["issue_number"] == 2 and new["generation"] == 1
    assert new["policy"]["task_budget_usd"] == 10
    assert controls.task_snapshot(first["task_id"]) == before
    assert admit_next("scheduler")["reason"] == "wip_limit"
    assert controls.status()["lanes"]["delivery"]["active"] == 2


def test_reconfigure_lower_lane_limit_waits_for_old_tasks_to_finish(db, policy):
    enable(policy)
    issue(1)
    first = admit_next("scheduler")["task_id"]
    changed = {**policy, "generation": 1, "max_tasks": 1}
    assert controls.set_control("configure", "operator", policy=changed)["ok"]
    issue(2, generation=1)
    assert admit_next("scheduler")["reason"] == "wip_limit"
    assert controls.can_start(first)["ok"]
    assert controls.finish_task(first, "cancelled", "operator")["ok"]
    assert admit_next("scheduler")["ok"]


def test_configure_and_admission_serialize_at_one_policy_cutoff(
    db, policy, monkeypatch
):
    monkeypatch.setenv("FACTORY_MAX_CONCURRENT_TASKS", "2")
    enable(policy)
    issue(1)
    issue(1, generation=1)
    barrier = Barrier(2)

    def configure():
        barrier.wait(timeout=3)
        return controls.set_control(
            "configure",
            "operator",
            policy={**policy, "generation": 1, "task_budget_usd": 10},
        )

    def admit():
        barrier.wait(timeout=3)
        return admit_next("scheduler")

    with ThreadPoolExecutor(max_workers=2) as pool:
        changed = pool.submit(configure)
        admitted = pool.submit(admit)
        assert changed.result(timeout=5)["ok"]
        result = admitted.result(timeout=5)
    assert result["ok"]
    snapshot = controls.task_snapshot(result["task_id"])
    assert snapshot["policy"]["generation"] == snapshot["generation"]
    assert snapshot["policy"]["task_budget_usd"] == (
        10 if snapshot["generation"] else 5
    )
    assert controls.status()["policy"]["generation"] == 1
    assert not admit_next("scheduler")["ok"]
    with Session(db) as session:
        assert len(session.exec(select(SwarmTask)).all()) == 1


def test_receipt_lookup_preserves_identity_and_requires_exact_generation(db):
    from factory.orchestration.factory_intake import get_issue_receipt

    first = issue(generation=2)
    db.dispose()
    result = get_issue_receipt("OWNER/REPO", 1, 2)
    assert result["created"] is False
    assert result["receipt"]["id"] == first["receipt"]["id"]
    assert get_issue_receipt("owner/repo", 1, 3) is None
    assert get_issue_receipt("other/repo", 1, 2) is None
    assert get_issue_receipt("owner/repo", 2, 2) is None


def test_receive_issue_still_writes_the_receipt_when_minting_fails(db, monkeypatch):
    from factory.orchestration import work_items

    def explode(*_args, **_kwargs):
        raise RuntimeError("mint exploded")

    monkeypatch.setattr(work_items, "mint_or_sync_from_github", explode)
    result = receive_issue(
        "owner/repo",
        41,
        "Unlinked",
        "body",
        "https://github.com/owner/repo/issues/41",
        "test",
        issue={"number": 41, "title": "Unlinked", "body": "body"},
    )
    assert result["created"] is True
    with Session(db) as session:
        receipt = session.get(FactoryReceipt, result["receipt"]["id"])
        assert receipt is not None
        assert receipt.work_item_id is None
