"""Hermetic work item pointer synchronization tests."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_controls, work_item_pointer, work_items
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEvent,
)
from factory.orchestration.models import SwarmTask


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'work-item-pointer.db'}",
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
                FactoryAudit,
                WorkItem,
                WorkItemEvent,
                FactoryReceipt,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="test"))
        session.commit()
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    monkeypatch.setattr(factory_controls, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def github_issue(number: int) -> dict:
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


def mint(db, number: int) -> int:
    with Session(db) as session:
        item, _outcome = work_items.mint_or_sync_from_github(
            session, "owner/repo", github_issue(number), actor="test"
        )
        session.commit()
        return item.id


def enable(monkeypatch):
    monkeypatch.setenv("FACTORY_WORK_ITEM_POINTER_ENABLED", "true")


def test_disabled_touches_nothing(db, monkeypatch):
    item_id = mint(db, 1)
    monkeypatch.delenv("FACTORY_WORK_ITEM_POINTER_ENABLED", raising=False)
    monkeypatch.setattr(
        work_item_pointer, "_github", lambda: pytest.fail("GitHub write attempted")
    )

    assert work_item_pointer.sync_pointers(actor="test") == {
        "created": 0,
        "updated": 0,
        "recreated": 0,
        "failed": 0,
        "skipped_disabled": 1,
    }
    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        assert item.github_pointer_comment_id is None
        assert item.pointer_synced_version == 0
        assert item.pointer_synced_at is None


def test_first_sync_creates_comment_and_stores_id(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)
    calls = []

    def write(*args, **kwargs):
        calls.append((args, kwargs))
        return {"id": 101}

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    counts = work_item_pointer.sync_pointers(actor="test")

    assert counts["created"] == 1
    assert calls[0][0][:2] == ("owner/repo", "issues/1/comments")
    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        assert item.github_pointer_comment_id == 101
        assert item.pointer_synced_version == 1
        assert item.pointer_synced_at is not None


def test_transition_updates_existing_comment(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)
    calls = []

    def write(repo, suffix, payload, **kwargs):
        calls.append((repo, suffix, payload, kwargs))
        return {"id": 101}

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    work_item_pointer.sync_pointers(actor="test")
    with Session(db) as session:
        work_items.transition(
            session,
            item_id,
            "ready",
            actor="test",
            author_kind="operator",
            cause_kind="test",
        )
        session.commit()

    counts = work_item_pointer.sync_pointers(actor="test")
    assert counts["updated"] == 1
    assert calls[-1][1] == "issues/comments/101"
    assert calls[-1][3] == {"method": "PATCH"}
    assert "(ready)" in calls[-1][2]["body"]


def test_field_only_sync_does_not_write(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)
    monkeypatch.setattr(
        work_item_pointer, "_github", lambda: lambda *_a, **_k: {"id": 1}
    )
    work_item_pointer.sync_pointers(actor="test")
    with Session(db) as session:
        session.add(
            WorkItemEvent(
                work_item_id=item_id,
                version=2,
                op="sync",
                author_kind="github",
                author="test",
                change_json='{"title":"changed"}',
                cause_kind="github_sync",
            )
        )
        session.commit()
    monkeypatch.setattr(
        work_item_pointer, "_github", lambda: pytest.fail("GitHub write attempted")
    )
    assert work_item_pointer.sync_pointers(actor="test")["updated"] == 0


def test_deleted_comment_is_recreated(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)
    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        item.github_pointer_comment_id = 404
        session.add(item)
        session.commit()
    calls = []

    def write(_repo, suffix, _payload, **kwargs):
        calls.append((suffix, kwargs))
        if kwargs.get("method") == "PATCH":
            request = httpx.Request("PATCH", "https://api.github.test/comment")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("deleted", request=request, response=response)
        return {"id": 405}

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    assert work_item_pointer.sync_pointers(actor="test")["recreated"] == 1
    assert calls == [
        ("issues/comments/404", {"method": "PATCH"}),
        ("issues/1/comments", {}),
    ]
    with Session(db) as session:
        assert session.get(WorkItem, item_id).github_pointer_comment_id == 405


def test_limit_bounds_one_pass(db, monkeypatch):
    for number in range(1, 26):
        mint(db, number)
    enable(monkeypatch)
    calls = []

    def write(_repo, suffix, _payload, **_kwargs):
        calls.append(suffix)
        return {"id": 1000 + len(calls)}

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    counts = work_item_pointer.sync_pointers(actor="test", limit=20)
    assert counts["created"] == len(calls) == 20
    with Session(db) as session:
        synced = session.exec(
            select(WorkItem).where(WorkItem.pointer_synced_version > 0)
        ).all()
        assert len(synced) == 20


def test_failure_is_audited_and_other_items_continue(db, monkeypatch):
    first = mint(db, 1)
    second = mint(db, 2)
    enable(monkeypatch)

    def write(_repo, suffix, _payload, **_kwargs):
        if suffix == "issues/1/comments":
            raise RuntimeError("GitHub unavailable")
        return {"id": 202}

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    counts = work_item_pointer.sync_pointers(actor="test")
    assert counts["failed"] == 1
    assert counts["created"] == 1
    with Session(db) as session:
        assert session.get(WorkItem, first).pointer_synced_version == 0
        assert session.get(WorkItem, second).pointer_synced_version == 1
        audit = session.exec(
            select(FactoryAudit).where(FactoryAudit.action == "work_item_pointer_error")
        ).one()
        assert audit.actor == "test"


def test_render_pointer_contains_marker_state_and_base_url(db, monkeypatch):
    item_id = mint(db, 7)
    monkeypatch.setenv("FACTORY_WORK_ITEM_BASE_URL", "https://factory.example")
    with Session(db) as session:
        text = work_item_pointer.render_pointer(session.get(WorkItem, item_id))
    assert f"<!-- work-item:{item_id} -->" in text
    assert f"work item **{item_id}** (open)" in text
    assert f"https://factory.example/factory/work-items/{item_id}" in text
    assert "pointer, not a mirror" in text


def test_pointer_version_ignores_field_events(db):
    item_id = mint(db, 1)
    with Session(db) as session:
        session.add_all(
            [
                WorkItemEvent(
                    work_item_id=item_id,
                    version=2,
                    op="sync",
                    author_kind="github",
                    author="test",
                    change_json="{}",
                    cause_kind="github_sync",
                ),
                WorkItemEvent(
                    work_item_id=item_id,
                    version=3,
                    op="transition",
                    author_kind="operator",
                    author="test",
                    change_json='{"state":"ready"}',
                    cause_kind="test",
                ),
                WorkItemEvent(
                    work_item_id=item_id,
                    version=4,
                    op="sync",
                    author_kind="github",
                    author="test",
                    change_json="{}",
                    cause_kind="github_sync",
                ),
            ]
        )
        session.flush()
        assert work_item_pointer.pointer_version(session, item_id) == 3


def test_failed_item_is_not_reselected_until_backoff_elapses(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)

    def write(_repo, suffix, _payload, **_kwargs):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    counts = work_item_pointer.sync_pointers(actor="test")
    assert counts["failed"] == 1

    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        assert item.pointer_failures == 1
        assert item.pointer_next_attempt_at is not None
        assert item.pointer_failures > 0

    monkeypatch.setattr(
        work_item_pointer, "_github", lambda: lambda *_a, **_k: {"id": 101}
    )
    counts = work_item_pointer.sync_pointers(actor="test")
    assert counts["created"] == 0
    assert counts["failed"] == 0

    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        assert item.pointer_synced_version == 0
        assert item.pointer_failures == 1


def test_successful_attempt_clears_backoff(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)
    call_count = 0

    def write(_repo, suffix, _payload, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("GitHub unavailable")
        return {"id": 101}

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    work_item_pointer.sync_pointers(actor="test")

    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        assert item.pointer_failures == 1
        assert item.pointer_next_attempt_at is not None

    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        item.pointer_next_attempt_at = None
        session.add(item)
        session.commit()

    counts = work_item_pointer.sync_pointers(actor="test")
    assert counts["created"] == 1

    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        assert item.pointer_failures == 0
        assert item.pointer_next_attempt_at is None
        assert item.pointer_synced_version > 0


def test_authority_change_advances_pointer(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)
    calls = []

    def write(repo, suffix, payload, **kwargs):
        calls.append((repo, suffix, payload, kwargs))
        return {"id": 101}

    monkeypatch.setattr(work_item_pointer, "_github", lambda: write)
    work_item_pointer.sync_pointers(actor="test")

    with Session(db) as session:
        work_items.transition(
            session,
            item_id,
            "ready",
            actor="test",
            author_kind="operator",
            cause_kind="test",
        )
        session.commit()

    counts = work_item_pointer.sync_pointers(actor="test")
    assert counts["updated"] == 1


def test_closed_item_not_selected(db, monkeypatch):
    item_id = mint(db, 1)
    enable(monkeypatch)

    with Session(db) as session:
        item = session.get(WorkItem, item_id)
        item.state = "closed"
        item.close_reason = "completed"
        session.add(item)
        session.commit()

    monkeypatch.setattr(
        work_item_pointer, "_github", lambda: pytest.fail("GitHub write attempted")
    )

    counts = work_item_pointer.sync_pointers(actor="test")
    assert counts["created"] == 0
    assert counts["failed"] == 0
