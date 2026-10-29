"""File-backed concurrency, private recording and non-admission regressions."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind
from pydantic import ValidationError
from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_controls as controls
from factory.orchestration import factory_intake, mcp, tracking, work_items
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryClassTier,
    FactoryControl,
    FactoryGithubIssueState,
    FactoryReceipt,
    FactoryReviewVerdict,
    FactoryStart,
    TrackingRequest,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmNodeRun, SwarmTask


@pytest.fixture
def engine(tmp_path, monkeypatch):
    value = create_engine(
        f"sqlite:///{tmp_path / 'tracking.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(value, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        value,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                FactoryControl,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryReceipt,
                FactoryStart,
                FactoryAudit,
                FactoryGithubIssueState,
                TrackingRequest,
                FactoryClassTier,
                FactoryReviewVerdict,
                SwarmNodeRun,
            )
        ],
    )
    with Session(value) as db:
        db.add(FactoryControl(id="factory", actor="test"))
        db.commit()
    monkeypatch.setattr(controls, "get_engine", lambda: value)
    monkeypatch.setattr("core.db.get_engine", lambda: value)
    yield value
    value.dispose()


def test_admission_recovery_refinement_and_dispatch_refuse_mislinked_tracking(
    engine, monkeypatch
):
    from factory.orchestration import (
        factory_conductor,
        factory_refine,
        factory_task_recovery,
    )

    item_id = tracking.record(engine, "joe", create())["item"]["id"]
    with Session(engine) as db:
        task = SwarmTask(
            id="test-tracking",
            task_text="forged",
            repo="owner/repo",
            conductor_model="opus",
        )
        db.add(task)
        db.flush()
        row = FactoryReceipt(
            repo="owner/repo",
            issue_number=1,
            title="forged",
            body="",
            url="https://github.com/owner/repo/issues/1",
            actor="test",
            work_item_id=item_id,
            task_id=task.id,
            state="admitted",
        )
        db.add(row)
        db.commit()
        control = db.get(FactoryControl, "factory")
        assert (
            controls._can_start(db, control, task.id)["reason"]
            == "work_item_not_executable"
        )
        with pytest.raises(ValueError, match="work_item_not_executable"):
            factory_task_recovery._capacity(db, control, row)
    monkeypatch.setattr(factory_conductor, "get_engine", lambda: engine)
    result = factory_conductor.reserve_node("test-tracking", "implement", "unused", {})
    assert not result.ok and result.refusal_code == "work_item_not_executable"
    factory_refine.reconcile({"id": "test-tracking"}, {}, [], [], 0)
    factory_refine._settle({"id": "test-tracking"}, {}, {})
    with Session(engine) as db:
        assert db.exec(select(FactoryStart)).all() == []
        assert db.exec(select(FactoryAudit)).all() == []
        assert db.exec(select(SwarmNodeRun)).all() == []
        assert len(tracking.read(engine, item_id)["events"]) == 1


def test_admission_poll_skips_injected_receipt_without_task_or_budget(engine):
    policy = {
        "repo": "owner/repo",
        "issue_numbers": [1],
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
    assert controls.set_control("configure", "operator", policy=policy)["ok"]
    assert controls.set_control("enable", "operator")["ok"]
    item_id = tracking.record(engine, "joe", create())["item"]["id"]
    with Session(engine) as db:
        db.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=1,
                title="forged",
                body="",
                url="https://github.com/owner/repo/issues/1",
                actor="operator",
                work_item_id=item_id,
            )
        )
        db.commit()
    result = factory_intake.admit_next("poller")
    assert not result["ok"]
    with Session(engine) as db:
        assert db.exec(select(SwarmTask)).all() == []
        assert db.exec(select(FactoryStart)).all() == []
        assert db.get(FactoryControl, "factory").admitted_count == 0


def test_source_collision_cannot_claim_factory_identity_or_write_outbound(
    engine, monkeypatch
):
    import httpx

    from factory.publication import _board_task_ids, _public_work_items

    def forbidden(*args, **kwargs):
        pytest.fail("recording must not call a network client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    source = "https://github.com/owner/repo/issues/7400"
    with Session(engine) as db:
        item = WorkItem(
            title="factory",
            state="ready",
            source_kind="github",
            source_ref=source,
            github_repo="owner/repo",
            github_issue_number=7400,
            trust="trusted",
        )
        db.add(item)
        db.commit()
        factory_id = item.id
    body = tracking.Create(**{**create().model_dump(), "source_ref": source})
    result = tracking.record(engine, "joe", body)
    assert result["item"]["id"] != factory_id
    with Session(engine) as db:
        assert db.get(WorkItem, factory_id).title == "factory"
        assert (
            db.exec(
                select(WorkItemEvent).where(WorkItemEvent.work_item_id == factory_id)
            ).all()
            == []
        )
        # SQLite permits this injected legacy link; PostgreSQL has a trigger.
        db.add(
            FactoryReceipt(
                repo="owner/repo",
                issue_number=7400,
                title="private title",
                body="private body",
                url=source,
                actor="forged",
                work_item_id=result["item"]["id"],
            )
        )
        db.commit()
        assert _board_task_ids(db) == (set(), {})
        public = _public_work_items(db, "2026-10-29T00:00:00Z")
        assert set(public) == {factory_id}
        assert "private" not in json.dumps(public)


def create(**overrides):
    return tracking.Create(
        request_key="create",
        title="External work",
        source_ref="https://example.test/work/1",
        tracking_kind="external",
        owner="Named owner",
        **overrides,
    )


def parallel(*calls):
    barrier = Barrier(len(calls))

    def call(function):
        barrier.wait(timeout=10)
        try:
            return function()
        except tracking.TrackingError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(call, calls))


def test_concurrent_identical_create_and_immutable_replay(engine):
    body = create()
    first, second = parallel(
        *(lambda: tracking.record(engine, "joe", body) for _ in range(2))
    )
    assert first == second
    item_id = first["item"]["id"]
    changed = tracking.record(
        engine,
        "joe",
        tracking.Change(
            request_key="complete",
            expected_version=1,
            state="done",
            completion_evidence="Owner confirmed",
        ),
        item_id=item_id,
    )
    assert changed["item"]["tracking_version"] == 2
    reopened = tracking.record(
        engine,
        "joe",
        tracking.Change(request_key="reopen", expected_version=2, state="open"),
        item_id=item_id,
    )
    assert reopened["item"]["completion_evidence"] == "Owner confirmed"
    assert reopened["item"]["completed_at"] == changed["item"]["completed_at"]
    assert tracking.record(engine, "joe", body) == first
    assert (
        tracking.record(
            engine,
            "joe",
            tracking.Change(
                request_key="complete",
                expected_version=1,
                state="done",
                completion_evidence="Owner confirmed",
            ),
            item_id=item_id,
        )
        == changed
    )
    events = tracking.read(engine, item_id)["events"]
    assert [e["version"] for e in events] == [3, 2, 1]
    assert {e["author"] for e in events} == {"joe"}


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_conflicting_create(engine, same_key):
    first = create()
    data = first.model_dump()
    if same_key:
        data["title"] = "conflict"
        data["source_ref"] = "https://example.test/work/2"
    else:
        data["request_key"] = "second"
    second = tracking.Create(**data)
    results = parallel(
        lambda: tracking.record(engine, "joe", first),
        lambda: tracking.record(engine, "joe", second),
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert (
        any(
            "request_key reused" in result if isinstance(result, str) else False
            for result in results
        )
        if same_key
        else any(isinstance(result, str) for result in results)
    )
    with Session(engine) as db:
        assert db.exec(select(func.count()).select_from(WorkItem)).one() == 1
        assert db.exec(select(func.count()).select_from(WorkItemEvent)).one() == 1
        assert db.exec(select(func.count()).select_from(TrackingRequest)).one() == 1


def test_canonical_source_and_identity_scope(engine):
    body = create()
    first = tracking.record(engine, "joe", body)
    same = tracking.Create(
        **{**body.model_dump(), "source_ref": "https://EXAMPLE.test:443/work/1#section"}
    )
    assert tracking.record(engine, "joe", same) == first
    with pytest.raises(tracking.TrackingError, match="source identity"):
        tracking.record(engine, "another-operator", body)


def test_concurrent_cas_has_one_winner_and_no_partial_events(engine):
    item_id = tracking.record(engine, "joe", create())["item"]["id"]
    results = parallel(
        *(
            lambda key=key: tracking.record(
                engine,
                "joe",
                tracking.Change(request_key=key, expected_version=1, state="active"),
                item_id=item_id,
            )
            for key in ("a", "b")
        )
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "stale expected_version" in results
    assert len(tracking.read(engine, item_id)["events"]) == 2
    with pytest.raises(tracking.TrackingError, match="stale"):
        tracking.record(
            engine,
            "joe",
            tracking.Change(request_key="stale", expected_version=1, state="ready"),
            item_id=item_id,
        )


def test_transitions_evidence_and_generic_paths(engine):
    item_id = tracking.record(engine, "joe", create())["item"]["id"]
    with pytest.raises(tracking.TrackingError, match="evidence"):
        tracking.record(
            engine,
            "joe",
            tracking.Change(request_key="bad", expected_version=1, state="done"),
            item_id=item_id,
        )
    for version, state in enumerate(
        ("ready", "active", "deferred", "needs_human", "open", "closed", "open"),
        start=1,
    ):
        tracking.record(
            engine,
            "joe",
            tracking.Change(
                request_key=f"v{version}", expected_version=version, state=state
            ),
            item_id=item_id,
        )
        with Session(engine) as db:
            assert not work_items.is_admissible(db, item_id)
            with pytest.raises(work_items.WorkItemError, match="tracking API"):
                work_items.transition(
                    db,
                    item_id,
                    "ready",
                    actor="forged",
                    author_kind="operator",
                    cause_kind="test",
                )
    tracking.record(
        engine,
        "joe",
        tracking.Change(request_key="close", expected_version=8, state="closed"),
        item_id=item_id,
    )
    with pytest.raises(tracking.TrackingError, match="reopen"):
        tracking.record(
            engine,
            "joe",
            tracking.Change(request_key="illegal", expected_version=9, state="active"),
            item_id=item_id,
        )
    with Session(engine) as db:
        assert db.exec(select(FactoryReceipt)).all() == []
        assert db.exec(select(SwarmTask)).all() == []
        assert db.exec(select(FactoryStart)).all() == []


def test_receipt_refuses_tracking_or_mislinked_reference_before_effects(engine):
    item_id = tracking.record(engine, "joe", create())["item"]["id"]
    for task_class in ("bug-fix", "refine"):
        with pytest.raises(ValueError, match="work_item_not_executable"):
            factory_intake.receive_issue(
                "owner/repo",
                7400,
                "title",
                "body",
                "https://github.com/owner/repo/issues/7400",
                "operator",
                work_item_id=item_id,
                task_class=task_class,
            )
    with Session(engine) as db:
        row = FactoryReceipt(
            repo="owner/repo",
            issue_number=7400,
            title="forged",
            body="",
            url="https://github.com/owner/repo/issues/7400",
            actor="test",
            work_item_id=item_id,
        )
        assert not controls.receipt_is_executable(db, row)
        assert db.exec(select(FactoryReceipt)).all() == []
        assert db.exec(select(FactoryAudit)).all() == []
        item = db.get(WorkItem, item_id)
        item.github_repo, item.github_issue_number = "owner/repo", 7400
        with pytest.raises(IntegrityError):
            db.commit()


def test_polling_publication_relationships_and_factory_unchanged(engine):
    from factory.orchestration.factory_intake_loop import _local_candidates
    from factory.publication import _public_work_items

    item_id = tracking.record(engine, "joe", create())["item"]["id"]
    with Session(engine) as db:
        factory = WorkItem(
            title="Executable",
            state="ready",
            source_kind="github",
            trust="trusted",
            github_repo="owner/repo",
            github_issue_number=7400,
        )
        db.add(factory)
        db.commit()
        assert work_items.is_admissible(db, factory.id)
        with pytest.raises(work_items.WorkItemError, match="relationships"):
            work_items.add_edge(
                db,
                item_id,
                factory.id,
                "blocks",
                actor="joe",
                author_kind="operator",
                cause_kind="test",
            )
        public = _public_work_items(db, "2026-10-29T00:00:00Z")
        assert item_id not in public and factory.id in public
        assert "Named owner" not in str(public)
    candidates, _, count = _local_candidates(
        "other/repo",
        intake={"exclude_labels": []},
        generation=0,
        room={"delivery": True, "advisory": True},
        cooldown_cutoff=datetime.now(UTC),
    )
    assert candidates == [] and count == 0


def test_pagination_filters_and_invalid_cursors(engine):
    for number in range(4):
        body = tracking.Create(
            request_key=f"create{number}",
            title="row",
            source_ref=f"https://example.test/{number}",
            owner="Owner",
            tracking_kind="manual" if number % 2 else "external",
        )
        tracking.record(engine, "joe", body)
    first = tracking.listing(engine, limit=2)
    second = tracking.listing(engine, limit=2, cursor=first["next_cursor"])
    assert len({row["id"] for row in first["items"] + second["items"]}) == 4
    assert second["next_cursor"] is None
    assert len(tracking.listing(engine, tracking_kind="manual")["items"]) == 2
    for args in (
        {"limit": 0},
        {"limit": True},
        {"state": "bad"},
        {"tracking_kind": "github"},
        {"cursor": "*:manual:4"},
        {"cursor": "*:*:-1"},
        {"cursor": "*:*:1;DROP TABLE"},
        {"cursor": "*:*:" + "9" * 99},
    ):
        with pytest.raises(tracking.TrackingError):
            tracking.listing(engine, **args)


@pytest.mark.parametrize(
    "change",
    [
        {"owner": " "},
        {"source_ref": "javascript:alert(1)"},
        {"source_ref": "https://user:secret@example.test"},
        {"source_ref": "https://example.test\\@other.test/task"},
        {"title": "x" * 513},
        {"actor": "joe"},
        {"execution_mode": "factory"},
        {"trust": "trusted"},
    ],
)
def test_untrusted_payload_cannot_supply_authority(change):
    with pytest.raises(ValidationError):
        tracking.Create(**{**create().model_dump(), **change})


@pytest.mark.parametrize(
    "authority,kind,groups",
    [
        (Authority.ANONYMOUS, PrincipalKind.HUMAN, ()),
        (Authority.STANDING, PrincipalKind.HUMAN, ()),
        (Authority.DELEGATED, PrincipalKind.HUMAN, ("operators",)),
        (Authority.STANDING, PrincipalKind.WORKLOAD, ("operators",)),
    ],
)
def test_all_mcp_operations_require_standing_human(engine, authority, kind, groups):
    principal = Principal(
        subject="forged",
        actor=("joe",),
        scope=("operator",),
        groups=groups,
        email="joe@example.test",
        kind=kind,
        authority=authority,
    )
    token = set_current_principal(principal)
    try:
        calls = (
            lambda: mcp.factory_tracking_create(create()),
            lambda: mcp.factory_tracking_read(1),
            lambda: mcp.factory_tracking_list(),
            lambda: mcp.factory_tracking_update(
                1,
                tracking.Change(request_key="update", expected_version=1, state="open"),
            ),
            lambda: mcp.factory_tracking_complete(
                1,
                tracking.Change(
                    request_key="complete",
                    expected_version=1,
                    state="done",
                    completion_evidence="claim",
                ),
            ),
        )
        for call in calls:
            assert asyncio.run(call())["ok"] is False
        with Session(engine) as db:
            assert db.exec(select(TrackingRequest)).all() == []
    finally:
        reset_current_principal(token)


def test_mcp_attributes_events_to_authenticated_subject(engine):
    principal = Principal(
        subject="verified-subject",
        actor=("untrusted-alias",),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    token = set_current_principal(principal)
    try:
        result = asyncio.run(mcp.factory_tracking_create(create()))
        item_id = result["item"]["id"]
        assert asyncio.run(mcp.factory_tracking_create(create())) == result
        detail = asyncio.run(mcp.factory_tracking_read(item_id))
        assert detail["events"][0]["author"] == "verified-subject"
        assert asyncio.run(mcp.factory_tracking_list())["items"][0]["id"] == item_id
        completed = asyncio.run(
            mcp.factory_tracking_complete(
                item_id,
                tracking.Change(
                    request_key="complete",
                    expected_version=1,
                    state="done",
                    completion_evidence="Owner confirmed completion",
                ),
            )
        )
        assert completed["item"]["state"] == "done"
        reopened = asyncio.run(
            mcp.factory_tracking_update(
                item_id,
                tracking.Change(request_key="reopen", expected_version=2, state="open"),
            )
        )
        assert reopened["item"]["completion_evidence"] == "Owner confirmed completion"
        assert asyncio.run(mcp.factory_tracking_create(create())) == result
    finally:
        reset_current_principal(token)
