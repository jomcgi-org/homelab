"""Unit tests for knowledge/mcp.py — MCP tools for knowledge search, notes, and tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gardener import GARDENER_VERSION
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


class TestCreateNoteTool:
    """Tests for the create_note MCP tool.

    ADR 006: create_note is fileless: it resolves a DB-unique note_id from
    the slugified title and indexes straight into Postgres, returning
    ``{"note_id", "path": "_processed/<id>.md"}``. The tests run against a
    real SQLite engine (``db_engine``) with the embedder mocked and assert on
    the persisted Note row rather than a file on disk.
    """

    @pytest.mark.asyncio
    async def test_creates_note_in_db(self, db_engine):
        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            result = await create_note(
                content="Some note body",
                title="My Test Note",
                tags=["test"],
                type="concept",
            )

        assert result["note_id"] == "my-test-note"
        assert result["path"] == "_processed/my-test-note.md"

        with Session(db_engine) as session:
            note = KnowledgeStore(session).get_note_by_id("my-test-note")
        assert note is not None
        assert note["title"] == "My Test Note"
        assert note["type"] == "concept"
        assert note["tags"] == ["test"]
        assert "Some note body" in note["content"]

    @pytest.mark.asyncio
    async def test_empty_content_returns_error(self):
        result = await create_note(content="")
        assert result == {"error": "content must not be empty"}

    @pytest.mark.asyncio
    async def test_whitespace_content_returns_error(self):
        result = await create_note(content="   \n  ")
        assert result == {"error": "content must not be empty"}

    @pytest.mark.asyncio
    async def test_collision_handling(self, db_engine):
        _insert_note(db_engine, note_id="my-note", title="My Note")

        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            result = await create_note(content="body", title="My Note")

        assert result["note_id"] == "my-note-1"
        assert result["path"] == "_processed/my-note-1.md"

    @pytest.mark.asyncio
    async def test_default_title_from_content(self, db_engine):
        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            result = await create_note(content="Short body")

        with Session(db_engine) as session:
            note = KnowledgeStore(session).get_note_by_id(result["note_id"])
        assert note["title"] == "Short body"

    @pytest.mark.asyncio
    async def test_visibility_arg_persisted(self, db_engine):
        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            result = await create_note(
                content="An atom about service mesh.",
                title="Linkerd mTLS",
                visibility="public",
            )

        row = _get_note_row(db_engine, result["note_id"])
        assert row.visibility == "public"

    @pytest.mark.asyncio
    async def test_rejects_invalid_visibility(self):
        result = await create_note(content="body", visibility="weird-value")
        assert "error" in result


class TestEditNoteTool:
    """Tests for the edit_note MCP tool.

    ADR 006: edit_note delegates to ``reindex_note_with_edits``, which loads
    the authoritative Note row from Postgres, merges the provided fields, and
    re-indexes in place (no vault file). The tests seed a real Note row, mock
    the embedder, and assert on the re-indexed row. A missing note returns
    ``{"error": "note not found: <id>"}`` (there is no missing-file path).
    """

    @pytest.mark.asyncio
    async def test_updates_content(self, db_engine):
        _insert_note(
            db_engine,
            note_id="n1",
            title="Original",
            content="Old body",
            path="papers/attention.md",
        )

        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            result = await edit_note("n1", content="New body", title="Updated Title")

        assert result == {"path": "papers/attention.md", "note_id": "n1"}

        with Session(db_engine) as session:
            note = KnowledgeStore(session).get_note_by_id("n1")
        assert note["title"] == "Updated Title"
        assert "New body" in note["content"]
        assert "Old body" not in note["content"]

    @pytest.mark.asyncio
    async def test_reindexes_into_postgres(self, db_engine):
        """ADR 006: an edit re-indexes synchronously into the note row."""
        _insert_note(db_engine, note_id="n1", title="Original", content="Old body")

        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            await edit_note("n1", content="Fresh body")

        with Session(db_engine) as session:
            note = KnowledgeStore(session).get_note_by_id("n1")
        assert "Fresh body" in note["content"]
        assert "Old body" not in note["content"]

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self, db_engine):
        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            result = await edit_note("nonexistent", content="x")

        assert result == {"error": "note not found: nonexistent"}

    @pytest.mark.asyncio
    async def test_preserves_existing_visibility(self, db_engine):
        """Title-only edit must not drop visibility from the row.

        Regression guard for the bug that nulled visibility on thousands of
        notes prior to this fix. Seed a note with visibility set and confirm
        a title-only edit preserves it on the re-indexed row.
        """
        _insert_note(
            db_engine,
            note_id="n1",
            title="Original",
            content="Old body",
            type="atom",
            visibility="public",
        )

        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            await edit_note("n1", title="Updated Title")

        row = _get_note_row(db_engine, "n1")
        assert row.visibility == "public"
        assert row.title == "Updated Title"

    @pytest.mark.asyncio
    async def test_sets_visibility_when_passed(self, db_engine):
        """Explicit visibility arg writes the field even if unset before."""
        _insert_note(db_engine, note_id="n1", title="Original", content="Old body")

        with (
            patch("knowledge.mcp.get_engine", return_value=db_engine),
            patch("knowledge.mcp.EmbeddingClient", return_value=_fake_embedder()),
        ):
            await edit_note("n1", visibility="private")

        row = _get_note_row(db_engine, "n1")
        assert row.visibility == "private"

    @pytest.mark.asyncio
    async def test_rejects_invalid_visibility(self):
        result = await edit_note("n1", visibility="weird")
        assert "error" in result


class TestDeleteNoteTool:
    """Tests for the delete_note MCP tool.

    ADR 006: the tool delegates to ``notes.delete_note(session, note_id)``
    (DB-only soft-delete, no vault_root, no file move). Soft-delete row
    semantics are covered in notes_test.py / notes_crud_test.py; here we only
    verify the MCP tool plumbing.
    """

    @pytest.mark.asyncio
    async def test_soft_deletes_via_notes_module(self):
        fake_note = MagicMock()
        fake_note.note_id = "n1"

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch(
                "knowledge.mcp.notes_module.delete_note", return_value=fake_note
            ) as mock_delete,
        ):
            result = await delete_note("n1")

        assert result == {"deleted": True, "note_id": "n1"}
        mock_delete.assert_called_once()
        call = mock_delete.call_args
        # Positional signature: notes.delete_note(session, note_id), no vault_root.
        assert call.args[1] == "n1"
        assert len(call.args) == 2

    @pytest.mark.asyncio
    async def test_value_error_returns_error_dict(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch(
                "knowledge.mcp.notes_module.delete_note",
                side_effect=ValueError("Note not found: note_id=missing"),
            ),
        ):
            result = await delete_note("missing")

        assert "error" in result
        assert "Note not found" in result["error"]


# ---------------------------------------------------------------------------
# Task tool tests
# ---------------------------------------------------------------------------

CANNED_TASKS = [
    {
        "note_id": "t1",
        "title": "Fix auth bug",
        "tags": ["backend"],
        "status": "todo",
        "due": "2026-04-20",
        "size": "small",
        "blocked_by": [],
        "task_completed": None,
    },
]


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


class TestListRawsNeedingDecomposition:
    """Tests for the list_raws_needing_decomposition MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_raws(self):
        fake_raw = SimpleNamespace(
            raw_id="r1",
            source="discord",
            created_at="2026-01-01",
        )
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.raws_needing_decomposition.return_value = [fake_raw]
            result = await list_raws_needing_decomposition()

        assert result["raws"] == [
            {
                "raw_id": "r1",
                "title": "r1",
                "source": "discord",
                "created_at": "2026-01-01",
            }
        ]

    @pytest.mark.asyncio
    async def test_clamps_limit(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.raws_needing_decomposition.return_value = []
            await list_raws_needing_decomposition(limit=999)

            MockStore.return_value.raws_needing_decomposition.assert_called_once_with(
                50
            )


class TestGetRaw:
    """Tests for the get_raw MCP tool."""

    @pytest.mark.asyncio
    async def test_found(self):
        row = SimpleNamespace(raw_id="r1", content_hash="r1", source="discord")
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.exec.return_value.first.return_value = row
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.fetch_raw", return_value="hello world"),
        ):
            result = await get_raw("r1")

        assert result == {
            "raw_id": "r1",
            "content": "hello world",
            "source": "discord",
        }

    @pytest.mark.asyncio
    async def test_missing(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.exec.return_value.first.return_value = None
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
        ):
            result = await get_raw("nope")

        assert "error" in result
        assert "nope" in result["error"]


class TestCreateAtom:
    """Tests for the create_atom MCP tool."""

    @pytest.mark.asyncio
    async def test_happy_path(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        index = AsyncMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.index_note_from_raw", index),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = None
            result = await create_atom(
                title="My Atom",
                body="The body.",
                type="atom",
                visibility="public",
                tags=["x"],
            )

        assert result == {"note_id": "my-atom"}
        index.assert_awaited_once()
        kwargs = index.call_args.kwargs
        assert kwargs["note_id"] == "my-atom"
        assert "visibility: public" in kwargs["raw"]
        assert "The body." in kwargs["raw"]

    @pytest.mark.asyncio
    async def test_scoped_assertion_fields_are_emitted(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        index = AsyncMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.index_note_from_raw", index),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = None
            result = await create_atom(
                title="Scoped Atom",
                body="The body.",
                type="fact",
                visibility="private",
                scope="session:abc",
                verification_state="verified",
                confidence=0.85,
                valid_from="2026-09-01T00:00:00Z",
                valid_until="2026-10-01T00:00:00Z",
                observed_at="2026-09-02T00:00:00Z",
            )

        assert result == {"note_id": "scoped-atom"}
        raw = index.call_args.kwargs["raw"]
        assert "scope: session:abc" in raw
        assert "verification_state: verified" in raw
        assert "confidence: 0.85" in raw
        assert "valid_from: '2026-09-01T00:00:00Z'" in raw
        assert "valid_until: '2026-10-01T00:00:00Z'" in raw
        assert "observed_at: '2026-09-02T00:00:00Z'" in raw

    @pytest.mark.asyncio
    async def test_rejects_bad_type(self):
        index = AsyncMock()
        with patch("knowledge.mcp.index_note_from_raw", index):
            result = await create_atom(
                title="x", body="y", type="bogus", visibility="public"
            )

        assert "error" in result
        index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_bad_visibility(self):
        index = AsyncMock()
        with patch("knowledge.mcp.index_note_from_raw", index):
            result = await create_atom(
                title="x", body="y", type="atom", visibility="secret"
            )

        assert "error" in result
        index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_bad_verification_state(self):
        index = AsyncMock()
        with patch("knowledge.mcp.index_note_from_raw", index):
            result = await create_atom(
                title="x",
                body="y",
                type="atom",
                visibility="public",
                verification_state="trusted",
            )

        assert "verification_state must be one of" in result["error"]
        index.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("confidence", [-0.01, 1.01])
    async def test_rejects_out_of_range_confidence(self, confidence):
        index = AsyncMock()
        with patch("knowledge.mcp.index_note_from_raw", index):
            result = await create_atom(
                title="x",
                body="y",
                type="atom",
                visibility="public",
                confidence=confidence,
            )

        assert "confidence must be between 0 and 1" in result["error"]
        index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_requires_status_and_size(self):
        index = AsyncMock()
        with patch("knowledge.mcp.index_note_from_raw", index):
            result = await create_atom(
                title="x", body="y", type="active", visibility="public"
            )

        assert "error" in result
        index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_with_status_and_size_ok(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        index = AsyncMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.index_note_from_raw", index),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = None
            result = await create_atom(
                title="Do The Thing",
                body="task body",
                type="active",
                visibility="private",
                status="active",
                size="small",
            )

        assert result == {"note_id": "do-the-thing"}
        kwargs = index.call_args.kwargs
        assert "status: active" in kwargs["raw"]
        assert "size: small" in kwargs["raw"]

    @pytest.mark.asyncio
    async def test_rejects_bad_edge_type(self):
        index = AsyncMock()
        with patch("knowledge.mcp.index_note_from_raw", index):
            result = await create_atom(
                title="x",
                body="y",
                type="atom",
                visibility="public",
                edges={"not_a_real_edge": ["target"]},
            )

        assert "error" in result
        index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_collision_appends_suffix(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        index = AsyncMock()

        def gnb(note_id):
            return SAMPLE_NOTE if note_id == "my-atom" else None

        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.index_note_from_raw", index),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.side_effect = gnb
            result = await create_atom(
                title="My Atom", body="b", type="atom", visibility="public"
            )

        assert result == {"note_id": "my-atom-1"}
        assert index.call_args.kwargs["note_id"] == "my-atom-1"

    @pytest.mark.asyncio
    async def test_records_provenance_when_raw_resolves(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.exec.return_value.first.return_value = SimpleNamespace(id=7)
        index = AsyncMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.index_note_from_raw", index),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = None
            result = await create_atom(
                title="My Atom",
                body="b",
                type="atom",
                visibility="public",
                derived_from_raw="r1",
            )

        assert result == {"note_id": "my-atom"}
        added = mock_session.add.call_args.args[0]
        assert isinstance(added, AtomRawProvenance)
        assert added.raw_fk == 7
        assert added.derived_note_id == "my-atom"
        assert added.gardener_version == GARDENER_VERSION
        mock_session.commit.assert_called_once()


class TestRecordProvenance:
    """Tests for the record_provenance MCP tool."""

    @pytest.mark.asyncio
    async def test_rejects_bad_outcome(self):
        result = await record_provenance("r1", "weird")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_raw_missing(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.exec.return_value.first.return_value = None
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
        ):
            result = await record_provenance("nope", "no-new-notes")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_no_new_notes(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.exec.return_value.first.return_value = SimpleNamespace(id=3)
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
        ):
            result = await record_provenance("r1", "no-new-notes")

        assert result == {"recorded": "no-new-notes", "raw_id": "r1"}
        added = mock_session.add.call_args.args[0]
        assert isinstance(added, AtomRawProvenance)
        assert added.raw_fk == 3
        assert added.derived_note_id == "no-new-notes"
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_inserts_new_row(self):
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.exec.side_effect = [
            _result(SimpleNamespace(id=4)),
            _result(None),
        ]
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
        ):
            result = await record_provenance("r1", "failed", error="boom")

        assert result == {"recorded": "failed", "raw_id": "r1"}
        added = mock_session.add.call_args.args[0]
        assert isinstance(added, AtomRawProvenance)
        assert added.derived_note_id == "failed"
        assert added.retry_count == 1
        assert added.error == "boom"
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_increments_existing_row(self):
        existing = SimpleNamespace(retry_count=2, error=None, gardener_version="old")
        mock_session = MagicMock()
        mock_session.__enter__.return_value = mock_session
        mock_session.exec.side_effect = [
            _result(SimpleNamespace(id=5)),
            _result(existing),
        ]
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
        ):
            result = await record_provenance("r1", "failed", error="again")

        assert result == {"recorded": "failed", "raw_id": "r1"}
        assert existing.retry_count == 3
        assert existing.error == "again"
        assert existing.gardener_version == GARDENER_VERSION
        mock_session.add.assert_called_once_with(existing)
        mock_session.commit.assert_called_once()
