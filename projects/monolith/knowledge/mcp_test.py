"""Unit tests for knowledge/mcp.py — MCP tools for knowledge search, notes, and tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.models import AtomRawProvenance, Note
from knowledge.mcp import (
    create_atom,
    create_note,
    delete_note,
    edit_note,
    get_daily_tasks,
    get_note,
    get_raw,
    get_weekly_tasks,
    list_raws_needing_decomposition,
    list_tasks,
    record_provenance,
    search_knowledge,
    search_tasks,
    update_task,
)
from knowledge.store import KnowledgeStore

FAKE_EMBEDDING = [0.1] * 1024

CANNED_RESULTS = [
    {
        "note_id": "n1",
        "title": "Attention Is All You Need",
        "path": "papers/attention.md",
        "type": "paper",
        "tags": ["ml", "transformers"],
        "score": 0.95,
        "section": "## Architecture",
        "snippet": "The transformer replaces recurrence entirely with attention.",
        "edges": [],
    },
]

SAMPLE_NOTE = {
    "note_id": "n1",
    "title": "Attention Is All You Need",
    "path": "papers/attention.md",
    "type": "paper",
    "tags": ["ml", "transformers"],
}


@pytest.fixture(name="db_engine")
def db_engine_fixture():
    """In-memory SQLite engine for the fileless create/edit write paths.

    ADR 006: create_note/edit_note index straight into Postgres, so these
    tools must run against a real DB rather than a mocked store. Strips the
    Postgres ``schema=`` overrides so ``create_all`` lands every table in the
    default SQLite schema, then restores them so the shared SQLModel.metadata
    isn't poisoned for other tests. StaticPool keeps a single connection so
    rows committed by the tool are visible to assertion sessions.
    """
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
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _fake_embedder() -> AsyncMock:
    """Embedder whose ``embed_batch`` returns deterministic 1024-dim vectors.

    The fileless write paths call ``EmbeddingClient().embed_batch`` during
    indexing; mocking it keeps the tests off the network.
    """
    client = AsyncMock()
    client.embed_batch.side_effect = lambda texts: [[0.1] * 1024 for _ in texts]
    return client


def _insert_note(
    engine,
    *,
    note_id: str,
    title: str = "Original",
    content: str | None = "Old body",
    path: str | None = None,
    type: str = "atom",
    visibility: str | None = None,
) -> None:
    """Insert a live Note row used by the edit/collision write-path tests."""
    with Session(engine) as session:
        session.add(
            Note(
                note_id=note_id,
                path=path or f"_processed/{note_id}.md",
                title=title,
                content_hash=f"hash-{note_id}",
                content=content,
                type=type,
                visibility=visibility,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()


def _get_note_row(engine, note_id: str) -> Note:
    """Fetch the raw Note ORM row (carries columns get_note_by_id omits)."""
    with Session(engine) as session:
        # test helper: intentionally reads any row (including soft-deleted).
        stmt = select(Note).where(
            Note.note_id == note_id
        )  # nosemgrep: sqlmodel-select-missing-deleted-at-filter
        return session.exec(stmt).one()


class TestSearchKnowledge:
    """Tests for the search_knowledge MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_results(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = (
                CANNED_RESULTS
            )
            result = await search_knowledge("attention")

        assert len(result["results"]) == 1
        assert result["results"][0]["note_id"] == "n1"
        mock_embed.embed.assert_awaited_once_with("attention")

    @pytest.mark.asyncio
    async def test_short_query_returns_empty(self):
        result = await search_knowledge("a")
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        result = await search_knowledge("")
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_limit_and_type_forwarded(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_notes_with_context.return_value = []
            await search_knowledge("attention", limit=5, type="paper")

            MockStore.return_value.search_notes_with_context.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                limit=5,
                type_filter="paper",
            )

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_error(self):
        mock_embed = AsyncMock()
        mock_embed.embed.side_effect = RuntimeError("boom")

        with (
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
        ):
            result = await search_knowledge("hello")

        assert "error" in result


class TestGetNote:
    """Tests for the get_note MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_note_with_content(self):
        """ADR 006: body comes from the authoritative Postgres ``content``."""
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": "# Attention\n\nSelf-attention mechanism.",
            }
            MockStore.return_value.get_note_links.return_value = []
            result = await get_note("n1")

        assert result["note_id"] == "n1"
        assert result["content"] == "# Attention\n\nSelf-attention mechanism."
        assert result["edges"] == []

    @pytest.mark.asyncio
    async def test_missing_note_returns_error(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = None
            result = await get_note("nonexistent")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_body_returns_error(self):
        """ADR 006: a row with a NULL ``content`` has no body to serve."""
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": None,
            }
            result = await get_note("n1")

        assert "error" in result
        assert "no body" in result["error"]


class TestListTasks:
    """Tests for the list_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_tasks(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks.return_value = CANNED_TASKS
            result = await list_tasks()

        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["note_id"] == "t1"
        MockStore.return_value.list_tasks.assert_called_once_with(
            statuses=None,
            due_before=None,
            due_after=None,
            sizes=None,
            include_someday=False,
        )

    @pytest.mark.asyncio
    async def test_forwards_filters(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks.return_value = []
            await list_tasks(
                status="todo,in-progress",
                due_before="2026-04-25",
                due_after="2026-04-18",
                size="small,medium",
                include_someday=True,
            )

            MockStore.return_value.list_tasks.assert_called_once_with(
                statuses=["todo", "in-progress"],
                due_before="2026-04-25",
                due_after="2026-04-18",
                sizes=["small", "medium"],
                include_someday=True,
            )


class TestSearchTasks:
    """Tests for the search_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_results(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_tasks.return_value = CANNED_TASKS
            result = await search_tasks("fix auth")

        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["note_id"] == "t1"

    @pytest.mark.asyncio
    async def test_short_query_returns_empty(self):
        result = await search_tasks("a")
        assert result == {"tasks": []}

    @pytest.mark.asyncio
    async def test_forwards_filters(self):
        mock_session = MagicMock()
        mock_embed = AsyncMock()
        mock_embed.embed.return_value = FAKE_EMBEDDING

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.search_tasks.return_value = []
            await search_tasks(
                "auth", status="todo,in-progress", include_someday=True, limit=5
            )

            MockStore.return_value.search_tasks.assert_called_once_with(
                query_embedding=FAKE_EMBEDDING,
                statuses=["todo", "in-progress"],
                include_someday=True,
                limit=5,
            )

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_error(self):
        mock_embed = AsyncMock()
        mock_embed.embed.side_effect = RuntimeError("boom")

        with (
            patch("knowledge.mcp.Session"),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=mock_embed),
        ):
            result = await search_tasks("hello world")

        assert "error" in result


class TestUpdateTask:
    """Tests for the update_task MCP tool."""

    @pytest.mark.asyncio
    async def test_successful_update(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            result = await update_task("t1", {"status": "done"})

        assert result == {"updated": True, "note_id": "t1"}
        MockStore.return_value.patch_task.assert_called_once_with(
            "t1", {"status": "done"}
        )

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.patch_task.side_effect = ValueError(
                "Task not found: nope"
            )
            result = await update_task("nope", {"status": "done"})

        assert result == {"error": "Task not found: nope"}


class TestGetDailyTasks:
    """Tests for the get_daily_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_daily_tasks(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks_daily.return_value = CANNED_TASKS
            result = await get_daily_tasks()

        assert len(result["tasks"]) == 1
        MockStore.return_value.list_tasks_daily.assert_called_once()


class TestGetWeeklyTasks:
    """Tests for the get_weekly_tasks MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_weekly_tasks(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.list_tasks_weekly.return_value = CANNED_TASKS
            result = await get_weekly_tasks()

        assert len(result["tasks"]) == 1
        MockStore.return_value.list_tasks_weekly.assert_called_once()


# ---------------------------------------------------------------------------
# Gardener decomposition tool tests (ADR 006 Phase 4c)
# ---------------------------------------------------------------------------


def _result(value):
    """Wrap a value as a SQLModel exec(...) result whose .first() returns it."""
    r = MagicMock()
    r.first.return_value = value
    return r
