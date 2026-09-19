"""Tests for work item body link parsing and reconciliation."""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from factory.orchestration import factory_controls
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    WorkItem,
    WorkItemEdge,
)
from factory.orchestration.work_item_links import parse_body_links, reconcile_body_edges


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Create an in-memory test database with schema support."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'work-item-links.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def receive_connect(dbapi_conn, connection_record):
        dbapi_conn.isolation_level = None

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="test"))
        session.commit()
        monkeypatch.setattr(factory_controls, "get_engine", lambda: engine)
        yield session
    engine.dispose()


def test_parse_body_links_blocked_by_phrases():
    """Test parsing of blocked_by phrases."""
    body = """
    This issue is blocked by #123.
    Also depends on #456.
    Waits on #789.
    """
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert 123 in result["blocked_by"]
    assert 456 in result["blocked_by"]
    assert 789 in result["blocked_by"]
    assert len(result["blocks"]) == 0


def test_parse_body_links_blocks_phrases():
    """Test parsing of blocks phrases."""
    body = "This issue blocks #100 and #200."
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert 100 in result["blocks"]
    assert 200 in result["blocks"]
    assert len(result["blocked_by"]) == 0


def test_parse_body_links_case_insensitive():
    """Test that parsing is case-insensitive."""
    body = "BLOCKED BY #123. Depends ON #456. WAITS ON #789. BLOCKS #999."
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert 123 in result["blocked_by"]
    assert 456 in result["blocked_by"]
    assert 789 in result["blocked_by"]
    assert 999 in result["blocks"]


def test_parse_body_links_same_repo_format():
    """Test parsing owner/repo#N format for same repo."""
    body = "Blocked by jomcgi-org/homelab#123. Blocks jomcgi-org/homelab#456."
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert 123 in result["blocked_by"]
    assert 456 in result["blocks"]


def test_parse_body_links_other_repo_ignored():
    """Test that other-repo references are ignored."""
    body = "Blocked by other-org/other-repo#123. Blocks #456."
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert 123 not in result["blocked_by"]
    assert 456 in result["blocks"]


def test_parse_body_links_fenced_code_ignored():
    """Test that matches in fenced code blocks are ignored."""
    body = """
    Blocked by #123.
    ```
    This mentions blocked by #999 and blocks #888.
    ```
    Blocks #456.
    """
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert 123 in result["blocked_by"]
    assert 456 in result["blocks"]
    assert 999 not in result["blocked_by"]
    assert 888 not in result["blocks"]


def test_parse_body_links_bare_number_ignored():
    """Test that bare #N without a phrase is ignored."""
    body = "See #100 for more details. This is blocked by #200."
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert 100 not in result["blocked_by"]
    assert 100 not in result["blocks"]
    assert 200 in result["blocked_by"]


def test_parse_body_links_multiple_numbers_per_phrase():
    """Test multiple numbers in one phrase."""
    body = "Blocked by #100, #200 and #300. Blocks #400, #500."
    result = parse_body_links(body, "jomcgi-org/homelab")
    assert result["blocked_by"] == {100, 200, 300}
    assert result["blocks"] == {400, 500}


@pytest.mark.parametrize(
    ("body", "blocked_by", "blocks"),
    [
        ("Blocked by #2, blocks #3.", {2}, {3}),
        ("It blocks #5 because #6 is broken.", set(), {5}),
        ("This unblocks #9.", set(), set()),
        ("blocked by #1 and #2, and also #3", {1, 2, 3}, set()),
        ("Blocked by owner/repo#4", {4}, set()),
        ("Blocked by other/repo#4", set(), set()),
    ],
)
def test_parse_body_links_bounds_each_phrase(body, blocked_by, blocks):
    assert parse_body_links(body, "owner/repo") == {
        "blocked_by": blocked_by,
        "blocks": blocks,
    }


def test_parse_body_links_empty_body():
    """Test with empty or None body."""
    assert parse_body_links("", "jomcgi-org/homelab") == {
        "blocked_by": set(),
        "blocks": set(),
    }
    assert parse_body_links(None, "jomcgi-org/homelab") == {
        "blocked_by": set(),
        "blocks": set(),
    }


def test_reconcile_body_edges_add_new_edges(db):
    """Test adding new edges from body text."""
    item1 = WorkItem(
        title="Issue 1",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=100,
        trust="trusted",
        body="Blocked by #200.",
    )
    item2 = WorkItem(
        title="Issue 2",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=200,
        trust="trusted",
        body="",
    )
    db.add(item1)
    db.add(item2)
    db.commit()
    db.refresh(item1)
    db.refresh(item2)

    items = [(item1, item1.body), (item2, item2.body)]
    result = reconcile_body_edges(db, "jomcgi-org/homelab", items, actor="system")

    assert result["added"] == 1
    assert result["removed"] == 0


def test_reconcile_body_edges_remove_stale_edges(db):
    """Test removing edges no longer mentioned in body."""
    item1 = WorkItem(
        title="Issue 1",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=100,
        trust="trusted",
        body="",
    )
    item2 = WorkItem(
        title="Issue 2",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=200,
        trust="trusted",
        body="",
    )
    db.add(item1)
    db.add(item2)
    db.commit()
    db.refresh(item1)
    db.refresh(item2)

    # Add a stale edge
    db.add(
        WorkItemEdge(
            from_id=item2.id, to_id=item1.id, kind="blocks", source="github_body"
        )
    )
    db.commit()

    items = [(item1, item1.body), (item2, item2.body)]
    result = reconcile_body_edges(db, "jomcgi-org/homelab", items, actor="system")

    assert result["removed"] == 1


def test_reconcile_body_edges_keeps_stale_edges_when_listing_is_truncated(db):
    item1 = WorkItem(
        title="Issue 1",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=100,
        trust="trusted",
    )
    item2 = WorkItem(
        title="Issue 2",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=200,
        trust="trusted",
    )
    db.add_all([item1, item2])
    db.flush()
    edge = WorkItemEdge(
        from_id=item2.id, to_id=item1.id, kind="blocks", source="github_body"
    )
    db.add(edge)
    db.commit()

    result = reconcile_body_edges(
        db,
        "jomcgi-org/homelab",
        [(item1, ""), (item2, "")],
        actor="system",
        truncated=True,
    )

    assert result["removed"] == 0
    assert result["removal_skipped_truncated"] == 1
    assert db.get(WorkItemEdge, edge.id) is not None


def test_reconcile_body_edges_audits_cycles_through_throttled_path(db):
    item1 = WorkItem(
        title="Issue 1",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=100,
        trust="trusted",
        body="Blocks #200.",
    )
    item2 = WorkItem(
        title="Issue 2",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=200,
        trust="trusted",
    )
    db.add_all([item1, item2])
    db.flush()
    db.add(
        WorkItemEdge(from_id=item2.id, to_id=item1.id, kind="blocks", source="manual")
    )
    db.commit()

    result = reconcile_body_edges(
        db,
        "jomcgi-org/homelab",
        [(item1, item1.body), (item2, item2.body)],
        actor="system",
    )
    db.commit()

    assert result["cycles"] == 1
    audits = db.exec(
        select(FactoryAudit).where(FactoryAudit.action == "work_item_edge_cycle")
    ).all()
    assert len(audits) == 1


def test_reconcile_body_edges_manual_untouched(db):
    """Test that manual edges are never touched."""
    item1 = WorkItem(
        title="Issue 1",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=100,
        trust="trusted",
        body="",
    )
    item2 = WorkItem(
        title="Issue 2",
        state="open",
        source_kind="github",
        authority="github",
        github_repo="jomcgi-org/homelab",
        github_issue_number=200,
        trust="trusted",
        body="",
    )
    db.add(item1)
    db.add(item2)
    db.commit()
    db.refresh(item1)
    db.refresh(item2)

    # Add a manual edge
    db.add(
        WorkItemEdge(from_id=item2.id, to_id=item1.id, kind="blocks", source="manual")
    )
    db.commit()

    items = [(item1, item1.body), (item2, item2.body)]
    result = reconcile_body_edges(db, "jomcgi-org/homelab", items, actor="system")

    # Manual edge should not be removed
    assert result["removed"] == 0


def test_reconcile_body_edges_local_items_skipped(db):
    """Test that local authority items are skipped."""
    item1 = WorkItem(
        title="Issue 1",
        state="open",
        source_kind="ui",
        authority="local",
        trust="trusted",
        body="Blocks #999.",
    )
    db.add(item1)
    db.commit()

    items = [(item1, item1.body)]
    result = reconcile_body_edges(db, "jomcgi-org/homelab", items, actor="system")

    assert result["skipped_local"] == 1
