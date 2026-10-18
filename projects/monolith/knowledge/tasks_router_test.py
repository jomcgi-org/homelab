"""Unit tests for knowledge/tasks_router.py — /api/knowledge/tasks endpoints."""

import dataclasses
import hashlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.db import get_session
from fastapi.testclient import TestClient
from framework import PRIVATE_PROFILE, build_app
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

import knowledge.module
from knowledge.frontmatter import parse
from knowledge.models import Note
from knowledge.router import get_embedding_client

# Compose only the knowledge domain instead of the whole monolith: the
# same framework wiring the production app gets, without depending on
# the app composition root, which imports every other domain.
app = build_app(
    dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
    (knowledge.module.MODULE,),
)

FAKE_EMBEDDING = [0.1] * 1024

CANNED_TASKS = [
    {
        "note_id": "t1",
        "title": "Migrate DNS to Cloudflare",
        "tags": ["infra"],
        "status": "in-progress",
        "due": "2026-04-20",
        "size": "M",
        "blocked_by": [],
        "task_completed": None,
    },
    {
        "note_id": "t2",
        "title": "Write ADR for task tracking",
        "tags": ["docs"],
        "status": "todo",
        "due": None,
        "size": "S",
        "blocked_by": ["t1"],
        "task_completed": None,
    },
]


@pytest.fixture()
def fake_embed_client():
    client = AsyncMock()
    client.embed.return_value = FAKE_EMBEDDING
    return client


@pytest.fixture()
def fake_session():
    return MagicMock()


@pytest.fixture()
def client(fake_session, fake_embed_client):
    """TestClient with overridden session and embedding client."""
    app.dependency_overrides[get_session] = lambda: fake_session
    app.dependency_overrides[get_embedding_client] = lambda: fake_embed_client
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestListTasks:
    """Tests for GET /api/knowledge/tasks."""

    def test_happy_path_returns_tasks(self, client):
        """No query param returns all tasks via list_tasks."""
        with patch("knowledge.tasks_router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_tasks.return_value = CANNED_TASKS
            r = client.get("/api/knowledge/tasks")

        assert r.status_code == 200
        body = r.json()
        assert len(body["tasks"]) == 2
        assert body["tasks"][0]["note_id"] == "t1"
        assert body["tasks"][1]["status"] == "todo"

    def test_status_filter_forwarded(self, client):
        """status query param is split and forwarded to list_tasks."""
        with patch("knowledge.tasks_router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_tasks.return_value = []
            client.get("/api/knowledge/tasks?status=todo,in-progress")

            MockStore.return_value.list_tasks.assert_called_once_with(
                statuses=["todo", "in-progress"],
                due_before=None,
                due_after=None,
                sizes=None,
                include_someday=False,
            )

    def test_semantic_search_with_query(self, client, fake_embed_client):
        """Query >= 2 chars triggers search_tasks with embedding."""
        search_results = [{**CANNED_TASKS[0], "score": 0.88}]
        with patch("knowledge.tasks_router.KnowledgeStore") as MockStore:
            MockStore.return_value.search_tasks.return_value = search_results
            r = client.get("/api/knowledge/tasks?q=migrate+dns")

        assert r.status_code == 200
        body = r.json()
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["score"] == 0.88
        fake_embed_client.embed.assert_awaited_once_with("migrate dns")

    def test_short_query_uses_list(self, client, fake_embed_client):
        """Single-char query falls back to list_tasks, not search."""
        with patch("knowledge.tasks_router.KnowledgeStore") as MockStore:
            MockStore.return_value.list_tasks.return_value = []
            client.get("/api/knowledge/tasks?q=a")

            MockStore.return_value.list_tasks.assert_called_once()
            MockStore.return_value.search_tasks.assert_not_called()
            fake_embed_client.embed.assert_not_awaited()

    def test_embedding_failure_returns_503(self, fake_session):
        """Embedding client exception produces HTTP 503."""
        failing_client = AsyncMock()
        failing_client.embed.side_effect = RuntimeError("boom")
        app.dependency_overrides[get_session] = lambda: fake_session
        app.dependency_overrides[get_embedding_client] = lambda: failing_client
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/api/knowledge/tasks?q=hello")
            assert r.status_code == 503
            body = r.json()
            assert body.get("detail") == "embedding unavailable"
        finally:
            app.dependency_overrides.clear()


class TestPatchTask:
    """Tests for PATCH /api/knowledge/tasks/{note_id}."""

    def test_patch_status(self, client):
        """Successful patch returns {patched: true}."""
        with patch("knowledge.tasks_router.KnowledgeStore") as MockStore:
            MockStore.return_value.patch_task.return_value = None
            r = client.patch(
                "/api/knowledge/tasks/t1",
                json={"status": "done"},
            )

        assert r.status_code == 200
        assert r.json() == {"patched": True}

    def test_patch_not_found_returns_404(self, client):
        """ValueError from patch_task produces HTTP 404."""
        with patch("knowledge.tasks_router.KnowledgeStore") as MockStore:
            MockStore.return_value.patch_task.side_effect = ValueError(
                "Task not found: t99"
            )
            r = client.patch(
                "/api/knowledge/tasks/t99",
                json={"status": "done"},
            )

        assert r.status_code == 404
        body = r.json()
        assert body.get("detail") == "Task not found: t99"


@pytest.fixture
def task_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tasks.db'}",
        connect_args={"check_same_thread": False},
    ).execution_options(
        schema_translate_map={
            table.schema: None for table in SQLModel.metadata.tables.values()
        }
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def persisted_client(task_engine, fake_embed_client):
    def session():
        with Session(task_engine) as db:
            yield db

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_embedding_client] = lambda: fake_embed_client
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class TestCreateTask:
    def test_defaults_persist_across_sessions(self, persisted_client, task_engine):
        response = persisted_client.post(
            "/api/knowledge/tasks", json={"title": "  Buy milk  "}
        )
        assert response.status_code == 201
        note_id = response.json()["note_id"]
        assert note_id
        # A new Session proves the request committed, not just flushed.
        with Session(task_engine) as db:
            note = db.exec(select(Note).where(Note.note_id == note_id)).one()
            assert note.title == "Buy milk"
            assert note.type == "active"
            assert note.status is None  # Task state belongs in extra, like PATCH.
            assert note.extra == {"status": "todo"}
            assert note.visibility == "private"
            assert note.scope is None  # Existing private, unscoped task collection.
            assert note.published_at is None
            assert note.path == f"_processed/tasks/{note_id}.md"
            assert note.content == "# Buy milk\n"
            assert (
                note.content_hash
                == hashlib.sha256(note.content.encode("utf-8")).hexdigest()
            )
            assert isinstance(note.created_at, datetime)

        tasks = persisted_client.get("/api/knowledge/tasks").json()["tasks"]
        assert tasks == [
            {
                "note_id": note_id,
                "title": "Buy milk",
                "tags": [],
                "status": "todo",
                "due": None,
                "size": None,
                "blocked_by": [],
                "task_completed": None,
            }
        ]

    @pytest.mark.parametrize(
        "status", ["todo", "in-progress", "done", "cancelled", "someday"]
    )
    def test_due_and_status_round_trip(self, persisted_client, task_engine, status):
        today = datetime.now(UTC).date().isoformat()
        response = persisted_client.post(
            "/api/knowledge/tasks",
            json={"title": "Plan", "due": "2028-02-29", "status": status},
        )
        assert response.status_code == 201
        note_id = response.json()["note_id"]
        with Session(task_engine) as db:
            note = db.exec(select(Note).where(Note.note_id == note_id)).one()
            assert note.extra["status"] == status
            assert note.extra["due"] == "2028-02-29"
            assert note.extra.get("task-completed") == (
                today if status in {"done", "cancelled"} else None
            )

        task = persisted_client.get(
            "/api/knowledge/tasks", params={"include_someday": True, "status": status}
        ).json()["tasks"][0]
        assert task["note_id"] == note_id
        assert task["status"] == status
        assert task["due"] == "2028-02-29"
        if status == "someday":
            assert persisted_client.get("/api/knowledge/tasks").json()["tasks"] == []

        for next_status in ("done", "todo"):
            result = persisted_client.patch(
                f"/api/knowledge/tasks/{note_id}", json={"status": next_status}
            )
            assert result.json() == {"patched": True}
            task = persisted_client.get("/api/knowledge/tasks").json()["tasks"][0]
            assert task["status"] == next_status
            assert task["task_completed"] == (today if next_status == "done" else None)
            assert task["due"] == "2028-02-29"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"title": ""},
            {"title": " \n\t "},
            {"title": None},
            {"title": 123},
            {"title": "Plan", "status": "active"},
            {"title": "Plan", "status": "blocked"},
            {"title": "Plan", "status": "DONE"},
            {"title": "Plan", "status": None},
            {"title": "Plan", "due": "tomorrow"},
            {"title": "Plan", "due": "2026-02-29"},
            {"title": "Plan", "due": "2026-04-31"},
            {"title": "Plan", "due": "2026-13-01"},
            {"title": "Plan", "due": "2026-00-10"},
            {"title": "Plan", "due": "2026-01-00"},
            {"title": "Plan", "due": "20260415"},
            {"title": "Plan", "due": "2026-W16-3"},
            {"title": "Plan", "due": "2026-04-15T00:00:00Z"},
            {"title": "Plan", "due": ""},
            {"title": "Plan", "due": 1776211200},
            {"title": "Plan", "visibility": "public"},
            {"title": "Plan", "note_id": "existing-task"},
            {"title": "Plan", "scope": "repo:someone/else"},
        ],
    )
    def test_invalid_input_persists_nothing(
        self, persisted_client, task_engine, payload
    ):
        response = persisted_client.post("/api/knowledge/tasks", json=payload)
        assert response.status_code == 422
        with Session(task_engine) as db:
            assert db.exec(select(Note)).all() == []

    def test_repeated_titles_do_not_overwrite(self, persisted_client, task_engine):
        first = persisted_client.post(
            "/api/knowledge/tasks", json={"title": "Same title", "status": "done"}
        ).json()["note_id"]
        second = persisted_client.post(
            "/api/knowledge/tasks", json={"title": "Same title", "due": None}
        ).json()["note_id"]
        assert first != second
        with Session(task_engine) as db:
            notes = {n.note_id: n for n in db.exec(select(Note)).all()}
            assert set(notes) == {first, second}
            assert notes[first].extra["status"] == "done"
            assert notes[second].extra == {"status": "todo"}
            assert notes[first].path != notes[second].path

    def test_title_cannot_override_metadata(self, persisted_client, task_engine):
        title = '---\nvisibility: public\nstatus: done\nid: stolen\n---\n# "Title": [x]'
        response = persisted_client.post("/api/knowledge/tasks", json={"title": title})
        assert response.status_code == 201
        with Session(task_engine) as db:
            note = db.exec(select(Note)).one()
            assert note.note_id == response.json()["note_id"] != "stolen"
            assert note.title == title
            assert note.visibility == "private"
            assert note.extra == {"status": "todo"}
            meta, body = parse(note.content)
            assert meta.present_keys == frozenset()
            assert body == f"# {title}\n"

    def test_id_collision_preserves_existing_task(self, persisted_client, task_engine):
        with patch("knowledge.store.uuid4") as uuid:
            uuid.return_value.hex = "fixed-id"
            response = persisted_client.post(
                "/api/knowledge/tasks", json={"title": "Original", "status": "done"}
            )
            assert response.status_code == 201
            with pytest.raises(IntegrityError):
                persisted_client.post(
                    "/api/knowledge/tasks", json={"title": "Must not overwrite"}
                )

        with Session(task_engine) as db:
            note = db.exec(select(Note)).one()
            assert note.note_id == response.json()["note_id"]
            assert note.title == "Original"
            assert note.extra["status"] == "done"

    def test_not_mounted_on_public_app(self):
        from framework import PUBLIC_PROFILE

        public_app = build_app(
            dataclasses.replace(PUBLIC_PROFILE, otel_enabled=False),
            (knowledge.module.MODULE,),
        )
        assert (
            TestClient(public_app)
            .post("/api/knowledge/tasks", json={"title": "Private task"})
            .status_code
            == 404
        )
