"""Unit tests for knowledge/tasks_router.py — /api/knowledge/tasks endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.db import get_session
from app.main import app
from knowledge.router import get_embedding_client

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
