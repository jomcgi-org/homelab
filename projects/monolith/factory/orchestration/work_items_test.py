"""Hermetic work item state, graph, synchronization, and API tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from auth.api import Authority, Principal, PrincipalKind, get_principal
from core.db import get_session
from factory.orchestration import factory_controls
from factory.orchestration.factory_models import (
    FactoryControl,
    FactoryGithubIssueState,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)
from factory.orchestration.factory_router import router
from factory.orchestration.models import SwarmTask
import factory.orchestration.work_items as work_items
from factory.orchestration.work_items import (
    WorkItemError,
    add_edge,
    close_missing_from_github,
    is_admissible,
    mint_or_sync_from_github,
    open_blockers,
    remove_edge,
    set_authority_local,
    state_from_github_labels,
    sync_github_work_items,
    transition,
    trust_for_github_author,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'work-items.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
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
                FactoryControl,
                WorkItem,
                WorkItemEdge,
                WorkItemEvent,
                FactoryReceipt,
                FactoryGithubIssueState,
            )
        ],
    )
    # The sweep entry point takes the factory control lock, so the row it
    # locks has to exist and the lock helper's engine has to be this one.
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="test"))
        session.commit()
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    monkeypatch.setattr(factory_controls, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def github_issue(number=1, labels=(), **overrides):
    value = {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "state": "open",
        "labels": [{"name": label} for label in labels],
        "user": {"login": "jomcgi", "type": "User"},
        "created_at": "2026-09-19T12:00:00Z",
        "updated_at": "2026-09-19T12:00:00Z",
    }
    value.update(overrides)
    return value


def local_item(session, *, state="open", authority="local", title="item"):
    item = WorkItem(
        title=title,
        state=state,
        close_reason="completed" if state == "closed" else None,
        closed_at=work_items._now() if state == "closed" else None,
        source_kind="factory",
        trust="trusted",
        authority=authority,
    )
    session.add(item)
    session.flush()
    return item


def event_rows(session, item_id):
    return session.exec(
        select(WorkItemEvent)
        .where(WorkItemEvent.work_item_id == item_id)
        .order_by(WorkItemEvent.version)
    ).all()


def op_kwargs(**overrides):
    return {
        "actor": "operator:test",
        "author_kind": "operator",
        "cause_kind": "test",
        "cause_ref": "test:1",
        "stated_reason": "test operation",
        **overrides,
    }


def test_work_item_edge_source_model_matches_migration_contract():
    source = WorkItemEdge.__table__.c.source
    assert source.nullable is False
    assert source.default.arg == "manual"
    constraint = next(
        constraint
        for constraint in WorkItemEdge.__table__.constraints
        if constraint.name == "work_item_edge_source_check"
    )
    assert str(constraint.sqltext) == "source IN ('manual','github_body','decision')"


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ({"login": "jomcgi", "type": "User"}, "trusted"),
        ({"login": "dependabot[bot]", "type": "Bot"}, "semi_trusted"),
        ({"login": "stranger", "type": "User"}, "untrusted"),
        (None, "untrusted"),
    ],
)
def test_github_author_trust(author, expected):
    assert trust_for_github_author(author) == expected


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({"needs-human", "agent-ready"}, "needs_human"),
        ({"needs-thought", "agent-ready"}, "deferred"),
        ({"agent-ready"}, "ready"),
        ({"unknown"}, "open"),
    ],
)
def test_github_label_state(labels, expected):
    assert state_from_github_labels(labels) == expected


def test_mint_from_github_and_backfill_receipt(db):
    with Session(db) as session:
        receipt = FactoryReceipt(
            repo="owner/repo",
            issue_number=1,
            title="receipt",
            body="old",
            url="https://github.com/owner/repo/issues/1",
            actor="poller",
        )
        session.add(receipt)
        session.flush()
        item, outcome = mint_or_sync_from_github(
            session,
            "owner/repo",
            github_issue(1, ["agent-ready", "documentation"]),
            actor="github-poller",
        )
        session.flush()
        session.refresh(receipt)
        assert outcome == "minted"
        assert item is not None
        assert item.title == "Issue 1"
        assert item.body == "body"
        assert item.state == "ready"
        assert item.task_class == "docs"
        assert item.labels == ["agent-ready", "documentation"]
        assert item.source_kind == "github"
        assert item.authority == "github"
        assert item.trust == "trusted"
        assert item.github_repo == "owner/repo"
        assert item.github_issue_number == 1
        assert receipt.work_item_id == item.id
        events = event_rows(session, item.id)
        assert [(row.version, row.op) for row in events] == [(1, "mint")]


def test_pull_request_entry_is_skipped(db):
    with Session(db) as session:
        item, outcome = mint_or_sync_from_github(
            session,
            "owner/repo",
            github_issue(pull_request={}),
            actor="poller",
        )
        assert item is None and outcome == "skipped"
        assert session.exec(select(WorkItem)).all() == []


def test_sync_writes_only_changed_fields_and_noop_writes_no_event(db):
    with Session(db) as session:
        item, _ = mint_or_sync_from_github(
            session, "owner/repo", github_issue(), actor="poller"
        )
        changed = github_issue(title="Revised")
        synced, outcome = mint_or_sync_from_github(
            session, "owner/repo", changed, actor="poller"
        )
        assert synced is item and outcome == "synced"
        assert json.loads(event_rows(session, item.id)[-1].change_json) == {
            "title": "Revised"
        }
        _same, outcome = mint_or_sync_from_github(
            session, "owner/repo", changed, actor="poller"
        )
        assert outcome == "unchanged"
        assert len(event_rows(session, item.id)) == 2


def test_local_authority_is_untouched_without_event(db):
    with Session(db) as session:
        item, _ = mint_or_sync_from_github(
            session, "owner/repo", github_issue(), actor="poller"
        )
        set_authority_local(session, item.id, **op_kwargs())
        before = len(event_rows(session, item.id))
        returned, outcome = mint_or_sync_from_github(
            session,
            "owner/repo",
            github_issue(title="GitHub replacement"),
            actor="poller",
        )
        assert returned.title == "Issue 1"
        assert outcome == "local_untouched"
        assert len(event_rows(session, item.id)) == before


@pytest.mark.parametrize("protected_state", ["active", "done"])
def test_sync_preserves_active_and_done_state_but_records_label_state(
    db, protected_state
):
    with Session(db) as session:
        item, _ = mint_or_sync_from_github(
            session, "owner/repo", github_issue(), actor="poller"
        )
        item.state = protected_state
        session.add(item)
        session.flush()
        _item, outcome = mint_or_sync_from_github(
            session,
            "owner/repo",
            github_issue(labels=["agent-ready"]),
            actor="poller",
        )
        assert outcome == "synced"
        assert item.state == protected_state
        change = json.loads(event_rows(session, item.id)[-1].change_json)
        assert change["label_state"] == "ready"
        assert "state" not in change


LEGAL_TRANSITIONS = [
    (source, target)
    for source, targets in work_items._TRANSITIONS.items()
    for target in targets
]


@pytest.mark.parametrize(("source", "target"), LEGAL_TRANSITIONS)
def test_all_legal_transitions_succeed(db, source, target):
    with Session(db) as session:
        item = local_item(session, state=source)
        result = transition(
            session,
            item.id,
            target,
            close_reason="completed" if target == "closed" else None,
            **op_kwargs(),
        )
        assert result.state == target
        assert (result.closed_at is not None) == (target == "closed")


@pytest.mark.parametrize(("source", "target"), [("open", "active"), ("done", "ready")])
def test_illegal_transitions_raise(db, source, target):
    with Session(db) as session:
        item = local_item(session, state=source)
        with pytest.raises(WorkItemError, match="illegal work item transition"):
            transition(session, item.id, target, **op_kwargs())


def test_closing_requires_reason_and_reopening_clears_close_fields(db):
    with Session(db) as session:
        item = local_item(session)
        with pytest.raises(WorkItemError, match="requires a valid close reason"):
            transition(session, item.id, "closed", **op_kwargs())
        transition(
            session, item.id, "closed", close_reason="not_planned", **op_kwargs()
        )
        assert item.close_reason == "not_planned" and item.closed_at is not None
        transition(session, item.id, "open", **op_kwargs())
        assert item.close_reason is None and item.closed_at is None


def test_duplicate_edge_is_noop_and_remove_is_idempotent(db):
    with Session(db) as session:
        first = local_item(session, title="first")
        second = local_item(session, title="second")
        add_edge(session, first.id, second.id, "blocks", **op_kwargs())
        add_edge(session, first.id, second.id, "blocks", **op_kwargs())
        assert len(session.exec(select(WorkItemEdge)).all()) == 1
        assert len(event_rows(session, first.id)) == 1
        remove_edge(session, first.id, second.id, "blocks", **op_kwargs())
        remove_edge(session, first.id, second.id, "blocks", **op_kwargs())
        assert session.exec(select(WorkItemEdge)).all() == []
        assert len(event_rows(session, first.id)) == 2


def test_two_and_three_node_blocks_cycles_raise(db):
    with Session(db) as session:
        a = local_item(session, title="a")
        b = local_item(session, title="b")
        c = local_item(session, title="c")
        add_edge(session, a.id, b.id, "blocks", **op_kwargs())
        with pytest.raises(WorkItemError, match="would create blocks cycle"):
            add_edge(session, b.id, a.id, "blocks", **op_kwargs())
        add_edge(session, b.id, c.id, "blocks", **op_kwargs())
        with pytest.raises(WorkItemError, match="would create blocks cycle"):
            add_edge(session, c.id, a.id, "blocks", **op_kwargs())


def test_supersedes_cycle_is_allowed(db):
    with Session(db) as session:
        a = local_item(session, title="a")
        b = local_item(session, title="b")
        add_edge(session, a.id, b.id, "supersedes", **op_kwargs())
        add_edge(session, b.id, a.id, "supersedes", **op_kwargs())
        assert len(session.exec(select(WorkItemEdge)).all()) == 2


def test_open_blockers_and_admissibility(db):
    with Session(db) as session:
        blocker = local_item(session, state="open", title="blocker")
        closed = local_item(session, state="closed", title="closed")
        target = local_item(session, state="ready", title="target")
        add_edge(session, blocker.id, target.id, "blocks", **op_kwargs())
        add_edge(session, closed.id, target.id, "blocks", **op_kwargs())
        assert [item.id for item in open_blockers(session, target.id)] == [blocker.id]
        assert not is_admissible(session, target.id)
        transition(
            session,
            blocker.id,
            "closed",
            close_reason="completed",
            **op_kwargs(),
        )
        assert is_admissible(session, target.id)
        target.state = "open"
        session.flush()
        assert not is_admissible(session, target.id)


def test_close_missing_only_closes_absent_github_authority_rows(db):
    with Session(db) as session:
        present, _ = mint_or_sync_from_github(
            session, "owner/repo", github_issue(1), actor="poller"
        )
        absent, _ = mint_or_sync_from_github(
            session, "owner/repo", github_issue(2), actor="poller"
        )
        local, _ = mint_or_sync_from_github(
            session, "owner/repo", github_issue(3), actor="poller"
        )
        set_authority_local(session, local.id, **op_kwargs())
        assert (
            close_missing_from_github(
                session, "owner/repo", {present.github_issue_number}, actor="poller"
            )
            == 1
        )
        assert present.state == "open"
        assert absent.state == "closed" and absent.close_reason == "github_closed"
        assert local.state == "open"


def test_truncated_sync_never_closes_missing_items(db):
    assert (
        sync_github_work_items(
            "owner/repo",
            [github_issue(1), github_issue(2)],
            truncated=False,
            actor="poller",
        )["minted"]
        == 2
    )
    counts = sync_github_work_items(
        "owner/repo", [github_issue(1)], truncated=True, actor="poller"
    )
    assert counts["closed"] == 0
    with Session(db) as session:
        second = session.exec(
            select(WorkItem).where(WorkItem.github_issue_number == 2)
        ).one()
        assert second.state == "open"


def test_source_ordered_sweep_cannot_overwrite_newer_snapshot(db):
    newer = github_issue(
        1,
        ["agent-ready"],
        title="Newer webhook state",
        updated_at="2026-09-19T12:02:00Z",
    )
    older = github_issue(
        1,
        [],
        title="Older sweep state",
        updated_at="2026-09-19T12:01:00Z",
    )
    assert (
        sync_github_work_items(
            "owner/repo",
            [newer],
            truncated=False,
            actor="github:webhook",
            source_ordered=True,
        )["minted"]
        == 1
    )
    counts = sync_github_work_items(
        "owner/repo",
        [older],
        truncated=False,
        actor="github:sweep",
        source_ordered=True,
    )
    assert counts["stale_ignored"] == 1
    assert counts["close_skipped"] == "source_ordered"
    with Session(db) as session:
        item = session.exec(select(WorkItem)).one()
        assert item.title == "Newer webhook state"
        assert item.state == "ready"


def operator_client(db):
    app = FastAPI()
    app.include_router(router)

    def session_override():
        with Session(db) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="operator:test",
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    return TestClient(app)


def test_work_item_routes_require_a_standing_operator(db):
    app = FastAPI()
    app.include_router(router)

    def session_override():
        with Session(db) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="anonymous",
        actor=(),
        scope=(),
        groups=(),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=Authority.ANONYMOUS,
    )
    client = TestClient(app)
    assert client.get("/api/swarm/factory/work-items").status_code == 403
    assert client.get("/api/swarm/factory/work-items/1").status_code == 403


def test_work_item_routes_get_list_filter_clamp_and_404(db):
    with Session(db) as session:
        rows = [
            WorkItem(
                title=f"item {index}",
                state="ready" if index % 2 else "open",
                source_kind="factory",
                trust="trusted",
                authority="local" if index % 3 else "github",
            )
            for index in range(205)
        ]
        session.add_all(rows)
        session.commit()
        item_id = rows[1].id
    client = operator_client(db)
    detail = client.get(f"/api/swarm/factory/work-items/{item_id}")
    assert detail.status_code == 200
    assert detail.json()["item"]["labels"] == []
    assert client.get("/api/swarm/factory/work-items/999999").status_code == 404
    assert (
        len(client.get("/api/swarm/factory/work-items?limit=999").json()["work_items"])
        == 200
    )
    filtered = client.get(
        "/api/swarm/factory/work-items?state=ready&authority=local&limit=999"
    ).json()["work_items"]
    assert filtered
    assert all(
        row["state"] == "ready" and row["authority"] == "local" for row in filtered
    )


def test_operator_work_item_reads_use_full_extracted_documents(db):
    with Session(db) as session:
        parent = local_item(session, title="parent")
        child = local_item(session, title="child")
        add_edge(session, parent.id, child.id, "parent", **op_kwargs())
        session.commit()
        child_id = child.id

    operator = operator_client(db)

    operator_detail = operator.get(f"/api/swarm/factory/work-items/{child_id}")
    assert operator_detail.status_code == 200
    assert operator_detail.json()["edges_in"][0]["kind"] == "parent"

    operator_list = operator.get("/api/swarm/factory/work-items").json()
    assert {item["title"] for item in operator_list["work_items"]} == {
        "parent",
        "child",
    }


def test_operator_and_browser_routes_serve_the_same_work_item_document(db):
    from factory.execution.router import router as agents_router

    with Session(db) as session:
        item = WorkItem(
            title="shared", state="ready", source_kind="factory", trust="trusted"
        )
        session.add(item)
        session.flush()
        older = FactoryReceipt(
            repo="owner/repo",
            issue_number=1,
            generation=0,
            title="older",
            body="body",
            url="https://github.com/owner/repo/issues/1",
            actor="test",
            task_class="docs",
            state="succeeded",
            work_item_id=item.id,
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        newer = FactoryReceipt(
            repo="owner/repo",
            issue_number=1,
            generation=1,
            title="newer",
            body="body",
            url="https://github.com/owner/repo/issues/1",
            actor="test",
            task_class="bug-fix",
            state="escalated",
            work_item_id=item.id,
            created_at=datetime.now(timezone.utc),
        )
        session.add_all([older, newer])
        session.commit()
        item_id = item.id
        older_id = older.id
        newer_id = newer.id
        newer_created_at = newer.created_at.isoformat()

    browser_app = FastAPI()
    browser_app.include_router(agents_router)

    def session_override():
        with Session(db) as session:
            yield session

    browser_app.dependency_overrides[get_session] = session_override
    browser = TestClient(browser_app)
    operator = operator_client(db)
    try:
        via_operator = operator.get(f"/api/swarm/factory/work-items/{item_id}").json()
        via_browser = browser.get(f"/api/agents/factory/work-items/{item_id}").json()
        assert via_operator == via_browser
        assert [receipt["id"] for receipt in via_operator["receipts"]] == [
            newer_id,
            older_id,
        ]
        assert via_operator["receipts"][0] == {
            "id": newer_id,
            "generation": 1,
            "task_class": "bug-fix",
            "state": "escalated",
            "created_at": newer_created_at,
            "task_id": None,
        }
        assert (
            operator.get("/api/swarm/factory/work-items?state=ready").json()
            == browser.get("/api/agents/factory/work-items?state=ready").json()
        )
    finally:
        browser_app.dependency_overrides.clear()
