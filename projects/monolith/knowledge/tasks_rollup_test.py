"""Unit tests for KnowledgeStore.list_tasks_daily() and list_tasks_weekly()."""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session

from knowledge.models import Note
from knowledge.store import KnowledgeStore
from shared.testing.sqlite import sqlite_session


@pytest.fixture(name="session")
def session_fixture():
    with sqlite_session() as session:
        yield session


def _make_task(
    session: Session,
    note_id: str,
    title: str = "Task",
    *,
    status: str = "active",
    due: str | None = None,
    size: str | None = None,
    tags: list[str] | None = None,
) -> Note:
    extra: dict = {"status": status}
    if due is not None:
        extra["due"] = due
    if size is not None:
        extra["size"] = size
    note = Note(
        note_id=note_id,
        path=f"_processed/tasks/{note_id}.md",
        title=title,
        content_hash=f"hash-{note_id}",
        type="active",
        tags=tags or [],
        extra=extra,
        indexed_at=datetime.now(timezone.utc),
    )
    session.add(note)
    session.commit()
    return note


# Pin "today" so tests are deterministic regardless of actual date.
FAKE_TODAY = date(2026, 4, 15)  # Wednesday


class TestListTasksDaily:
    @patch("knowledge.store.date")
    def test_daily_includes_due_today(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        _make_task(session, "t1", "Due today", due="2026-04-15")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_daily()
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t1"

    @patch("knowledge.store.date")
    def test_daily_includes_overdue(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        _make_task(session, "t1", "Overdue", due="2026-04-10")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_daily()
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t1"

    @patch("knowledge.store.date")
    def test_daily_excludes_future(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        _make_task(session, "t1", "Tomorrow", due="2026-04-16")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_daily()
        assert len(tasks) == 0

    @patch("knowledge.store.date")
    def test_daily_excludes_someday(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        _make_task(session, "t1", "Someday task", status="someday", due="2026-04-15")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_daily()
        assert len(tasks) == 0


class TestListTasksWeekly:
    @patch("knowledge.store.date")
    def test_weekly_includes_this_week(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY  # Wednesday
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        # Sunday of the same week is 2026-04-19
        _make_task(session, "t1", "Due Friday", due="2026-04-17")
        _make_task(session, "t2", "Due Sunday", due="2026-04-19")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_weekly()
        ids = {t["note_id"] for t in tasks}
        assert ids == {"t1", "t2"}

    @patch("knowledge.store.date")
    def test_weekly_excludes_next_week(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY  # Wednesday
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        # Monday of next week
        _make_task(session, "t1", "Next Monday", due="2026-04-20")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_weekly()
        assert len(tasks) == 0

    @patch("knowledge.store.date")
    def test_weekly_includes_overdue(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        _make_task(session, "t1", "Past due", due="2026-04-10")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_weekly()
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t1"

    @patch("knowledge.store.date")
    def test_weekly_excludes_someday(self, mock_date, session):
        mock_date.today.return_value = FAKE_TODAY
        mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
        _make_task(session, "t1", "Someday", status="someday", due="2026-04-17")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_weekly()
        assert len(tasks) == 0
