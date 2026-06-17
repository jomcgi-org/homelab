"""Unit tests for knowledge/mcp.py — MCP tools for knowledge search, notes, and tasks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge.gardener import GARDENER_VERSION
from knowledge.models import AtomRawProvenance
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
    async def test_returns_note_with_content(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        note_file = vault_dir / "papers" / "attention.md"
        note_file.parent.mkdir(parents=True)
        note_file.write_text("# Attention\n\nSelf-attention mechanism.")

        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = SAMPLE_NOTE
            MockStore.return_value.get_note_links.return_value = []
            result = await get_note("n1")

        assert result["note_id"] == "n1"
        assert result["content"] == "# Attention\n\nSelf-attention mechanism."
        assert result["edges"] == []

    @pytest.mark.asyncio
    async def test_serves_content_from_db_without_disk(self, tmp_path, monkeypatch):
        """ADR 006 Phase 2: body comes from Postgres, no vault file read."""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()  # empty — proves the read does not touch disk
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "content": "# From Postgres\n\nNo disk read needed.",
            }
            MockStore.return_value.get_note_links.return_value = []
            result = await get_note("n1")

        assert result["content"] == "# From Postgres\n\nNo disk read needed."
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
    async def test_missing_vault_file_returns_error(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "path": "nonexistent/missing.md",
            }
            result = await get_note("n1")

        assert "error" in result


class TestCreateNoteTool:
    """Tests for the create_note MCP tool."""

    @pytest.mark.asyncio
    async def test_creates_file(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        result = await create_note(
            content="Some note body",
            title="My Test Note",
            tags=["test"],
            type="concept",
        )

        assert "path" in result
        assert result["path"] == "my-test-note.md"
        created = vault_dir / result["path"]
        assert created.is_file()
        text = created.read_text()
        assert "title: My Test Note" in text
        assert "Some note body" in text

    @pytest.mark.asyncio
    async def test_empty_content_returns_error(self):
        result = await create_note(content="")
        assert result == {"error": "content must not be empty"}

    @pytest.mark.asyncio
    async def test_whitespace_content_returns_error(self):
        result = await create_note(content="   \n  ")
        assert result == {"error": "content must not be empty"}

    @pytest.mark.asyncio
    async def test_collision_handling(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        (vault_dir / "my-note.md").write_text("existing")

        result = await create_note(content="body", title="My Note")
        assert result["path"] == "my-note-1.md"

    @pytest.mark.asyncio
    async def test_default_title_from_content(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        result = await create_note(content="Short body")
        created = vault_dir / result["path"]
        text = created.read_text()
        assert "title: Short body" in text

    @pytest.mark.asyncio
    async def test_visibility_arg_writes_frontmatter(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        result = await create_note(
            content="An atom about service mesh.",
            title="Linkerd mTLS",
            visibility="public",
        )

        text = (vault_dir / result["path"]).read_text()
        assert "visibility: public" in text

    @pytest.mark.asyncio
    async def test_rejects_invalid_visibility(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        result = await create_note(content="body", visibility="weird-value")
        assert "error" in result


class TestEditNoteTool:
    """Tests for the edit_note MCP tool."""

    @pytest.mark.asyncio
    async def test_updates_content(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        note_file = vault_dir / "papers" / "attention.md"
        note_file.parent.mkdir(parents=True)
        note_file.write_text("---\ntitle: Original\n---\nOld body")

        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = SAMPLE_NOTE
            result = await edit_note("n1", content="New body", title="Updated Title")

        assert result == {"path": "papers/attention.md", "note_id": "n1"}
        text = note_file.read_text()
        assert "title: Updated Title" in text
        assert "New body" in text
        assert "Old body" not in text

    @pytest.mark.asyncio
    async def test_reindexes_into_postgres(self, tmp_path, monkeypatch):
        """ADR 006 Phase 3: an edit re-indexes synchronously via upsert_note."""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        note_file = vault_dir / "papers" / "attention.md"
        note_file.parent.mkdir(parents=True)
        note_file.write_text("---\nid: n1\ntitle: Original\n---\nOld body")
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        embed = AsyncMock()
        embed.embed_batch.side_effect = lambda texts: [[0.1] * 1024 for _ in texts]
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=embed),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = SAMPLE_NOTE
            await edit_note("n1", content="Fresh body")

        MockStore.return_value.upsert_note.assert_called_once()
        kwargs = MockStore.return_value.upsert_note.call_args.kwargs
        assert kwargs["note_id"] == "n1"
        assert "Fresh body" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_not_found_returns_error(self):
        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = None
            result = await edit_note("nonexistent", content="x")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_file_returns_error(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = {
                **SAMPLE_NOTE,
                "path": "gone/missing.md",
            }
            result = await edit_note("n1", content="x")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_preserves_existing_visibility(self, tmp_path, monkeypatch):
        """Title-only edit must not drop visibility from frontmatter.

        Regression guard for the bug that nulled visibility on thousands of
        notes prior to this fix. Mirror the file shape that the gardener
        emits (id + title + type + visibility) and confirm the rewrite
        keeps the visibility line.
        """
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        note_file = vault_dir / "papers" / "attention.md"
        note_file.parent.mkdir(parents=True)
        note_file.write_text(
            "---\nid: n1\ntitle: Original\ntype: atom\nvisibility: public\n---\nOld body"
        )

        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = SAMPLE_NOTE
            await edit_note("n1", title="Updated Title")

        text = note_file.read_text()
        assert "visibility: public" in text
        assert "title: Updated Title" in text

    @pytest.mark.asyncio
    async def test_sets_visibility_when_passed(self, tmp_path, monkeypatch):
        """Explicit visibility arg writes the field even if missing before."""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        note_file = vault_dir / "papers" / "attention.md"
        note_file.parent.mkdir(parents=True)
        note_file.write_text("---\nid: n1\ntitle: Original\n---\nOld body")

        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

        mock_session = MagicMock()
        with (
            patch("knowledge.mcp.Session", return_value=mock_session),
            patch("knowledge.mcp.get_engine"),
            patch("knowledge.mcp.EmbeddingClient", return_value=AsyncMock()),
            patch("knowledge.mcp.KnowledgeStore") as MockStore,
        ):
            MockStore.return_value.get_note_by_id.return_value = SAMPLE_NOTE
            await edit_note("n1", visibility="private")

        assert "visibility: private" in note_file.read_text()

    @pytest.mark.asyncio
    async def test_rejects_invalid_visibility(self, tmp_path, monkeypatch):
        result = await edit_note("n1", visibility="weird")
        assert "error" in result


class TestDeleteNoteTool:
    """Tests for the delete_note MCP tool.

    The tool now delegates to notes.delete_note (soft-delete) instead of
    the old hard-delete path. File-on-disk semantics, missing-file
    tolerance, and Trash bookkeeping are all covered in notes_test.py;
    here we only verify the MCP tool plumbing.
    """

    @pytest.mark.asyncio
    async def test_soft_deletes_via_notes_module(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

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
        # Positional signature: notes.delete_note(session, note_id, vault_root)
        assert call.args[1] == "n1"
        assert call.args[2] == vault_dir.resolve()

    @pytest.mark.asyncio
    async def test_value_error_returns_error_dict(self, tmp_path, monkeypatch):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        monkeypatch.setenv("VAULT_ROOT", str(vault_dir))

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
