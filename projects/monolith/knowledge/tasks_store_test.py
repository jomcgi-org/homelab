"""Unit tests for KnowledgeStore.list_tasks() and patch_task()."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.models import Note
from knowledge.store import KnowledgeStore


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _make_task(
    session: Session,
    note_id: str,
    title: str = "Task",
    *,
    status: str = "active",
    due: str | None = None,
    size: str | None = None,
    tags: list[str] | None = None,
    blocked_by: list[str] | None = None,
    note_type: str = "active",
) -> Note:
    extra: dict = {"status": status}
    if due is not None:
        extra["due"] = due
    if size is not None:
        extra["size"] = size
    if blocked_by is not None:
        extra["blocked-by"] = blocked_by
    note = Note(
        note_id=note_id,
        path=f"_processed/tasks/{note_id}.md",
        title=title,
        content_hash=f"hash-{note_id}",
        type=note_type,
        tags=tags or [],
        extra=extra,
        indexed_at=datetime.now(timezone.utc),
    )
    session.add(note)
    session.commit()
    return note


class TestListTasks:
    def test_returns_only_active_type_notes(self, session):
        _make_task(session, "t1", "Active Task", note_type="active")
        _make_task(session, "t2", "Atom Note", note_type="atom")

        store = KnowledgeStore(session)
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t1"

    def test_filters_by_status(self, session):
        _make_task(session, "t1", status="active")
        _make_task(session, "t2", status="blocked")
        _make_task(session, "t3", status="done")

        store = KnowledgeStore(session)
        tasks = store.list_tasks(statuses=["active", "blocked"])
        assert len(tasks) == 2
        ids = {t["note_id"] for t in tasks}
        assert ids == {"t1", "t2"}

    def test_filters_by_due_before(self, session):
        _make_task(session, "t1", due="2026-04-10")
        _make_task(session, "t2", due="2026-04-20")
        _make_task(session, "t3")  # no due date

        store = KnowledgeStore(session)
        tasks = store.list_tasks(due_before="2026-04-15")
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t1"

    def test_due_before_is_inclusive(self, session):
        _make_task(session, "t1", due="2026-04-15")

        store = KnowledgeStore(session)
        tasks = store.list_tasks(due_before="2026-04-15")
        assert len(tasks) == 1

    def test_filters_by_due_after(self, session):
        _make_task(session, "t1", due="2026-04-10")
        _make_task(session, "t2", due="2026-04-20")
        _make_task(session, "t3")  # no due date

        store = KnowledgeStore(session)
        tasks = store.list_tasks(due_after="2026-04-15")
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t2"

    def test_filters_by_size(self, session):
        _make_task(session, "t1", size="small")
        _make_task(session, "t2", size="large")
        _make_task(session, "t3")  # no size

        store = KnowledgeStore(session)
        tasks = store.list_tasks(sizes=["small"])
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t1"

    def test_excludes_someday_by_default(self, session):
        _make_task(session, "t1", status="active")
        _make_task(session, "t2", status="someday")

        store = KnowledgeStore(session)
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["note_id"] == "t1"

    def test_includes_someday_when_requested(self, session):
        _make_task(session, "t1", status="active")
        _make_task(session, "t2", status="someday")

        store = KnowledgeStore(session)
        tasks = store.list_tasks(include_someday=True)
        assert len(tasks) == 2
        ids = {t["note_id"] for t in tasks}
        assert ids == {"t1", "t2"}

    def test_exclude_statuses_drops_listed_and_keeps_blank(self, session):
        _make_task(session, "t1", status="todo")
        _make_task(session, "t2", status="done")
        _make_task(session, "t3", status="cancelled")
        _make_task(session, "t4", status="")  # blank status must survive

        store = KnowledgeStore(session)
        tasks = store.list_tasks(exclude_statuses={"done", "cancelled"})
        ids = {t["note_id"] for t in tasks}
        assert ids == {"t1", "t4"}

    def test_returns_task_fields(self, session):
        _make_task(
            session,
            "t1",
            "My Task",
            status="active",
            due="2026-05-01",
            size="medium",
            tags=["project-x"],
            blocked_by=["t0"],
        )

        store = KnowledgeStore(session)
        tasks = store.list_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task["note_id"] == "t1"
        assert task["title"] == "My Task"
        assert task["tags"] == ["project-x"]
        assert task["status"] == "active"
        assert task["due"] == "2026-05-01"
        assert task["size"] == "medium"
        assert task["blocked_by"] == ["t0"]
        assert task["task_completed"] is None


class TestDailyWeeklyHideFinished:
    """The daily/weekly views (and the MCP tools behind them) must not show
    tasks that are already done or cancelled, however overdue they are."""

    def test_daily_hides_done_and_cancelled_but_keeps_open_overdue(self, session):
        # All overdue (due long before today), so due_before is satisfied.
        _make_task(session, "open", status="todo", due="2020-01-01")
        _make_task(session, "finished", status="done", due="2020-01-01")
        _make_task(session, "dropped", status="cancelled", due="2020-01-01")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_daily()
        ids = {t["note_id"] for t in tasks}
        assert ids == {"open"}

    def test_weekly_hides_done_and_cancelled(self, session):
        _make_task(session, "open", status="todo", due="2020-01-01")
        _make_task(session, "finished", status="done", due="2020-01-01")

        store = KnowledgeStore(session)
        tasks = store.list_tasks_weekly()
        ids = {t["note_id"] for t in tasks}
        assert ids == {"open"}


class TestPatchTask:
    def test_patch_status_to_done_sets_completed_date(self, session):
        _make_task(session, "t1", status="active")

        store = KnowledgeStore(session)
        store.patch_task("t1", {"status": "done"})

        tasks = store.list_tasks(statuses=["done"])
        assert len(tasks) == 1
        assert tasks[0]["task_completed"] is not None
        # Verify it's a valid date string.
        datetime.strptime(tasks[0]["task_completed"], "%Y-%m-%d")

    def test_patch_arbitrary_fields(self, session):
        _make_task(session, "t1", status="active")

        store = KnowledgeStore(session)
        store.patch_task("t1", {"size": "large", "due": "2026-06-01"})

        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["size"] == "large"
        assert tasks[0]["due"] == "2026-06-01"

    def test_patch_nonexistent_task_raises(self, session):
        store = KnowledgeStore(session)
        with pytest.raises(ValueError, match="Task not found"):
            store.patch_task("nonexistent", {"status": "done"})

    def test_patch_done_to_todo_clears_completed(self, session):
        _make_task(session, "t1", status="active")

        store = KnowledgeStore(session)
        store.patch_task("t1", {"status": "done"})
        # Verify completed is set.
        tasks = store.list_tasks(statuses=["done"])
        assert tasks[0]["task_completed"] is not None

        # Move back to active — completed should be cleared.
        store.patch_task("t1", {"status": "active"})
        tasks = store.list_tasks(statuses=["active"])
        assert len(tasks) == 1
        assert tasks[0]["task_completed"] is None
