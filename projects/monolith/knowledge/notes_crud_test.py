"""Unit tests for knowledge notes CRUD endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session
from app.main import app
from knowledge.models import Note
from knowledge.router import get_embedding_client
from knowledge.service import VAULT_ROOT_ENV


@pytest.fixture()
def fake_session():
    return MagicMock()


@pytest.fixture()
def fake_embed_client():
    """Embedding client whose embed_batch returns deterministic vectors.

    ADR 006 Phase 3: edit_note re-indexes synchronously, so the write-path
    tests need the embedder overridden to avoid a real network call.
    """
    client = AsyncMock()
    client.embed_batch.side_effect = lambda texts: [[0.1] * 1024 for _ in texts]
    return client


@pytest.fixture()
def client(fake_session, fake_embed_client, tmp_path, monkeypatch):
    """TestClient with overridden session, embedder, and a temp vault root."""
    monkeypatch.setenv(VAULT_ROOT_ENV, str(tmp_path))
    app.dependency_overrides[get_session] = lambda: fake_session
    app.dependency_overrides[get_embedding_client] = lambda: fake_embed_client
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture()
def real_session():
    """Real SQLite session for tests that exercise the DB instead of mocking it.

    Mirrors the fixture in gap_review_endpoints_test.py: strips Postgres-
    only schema= overrides so create_all() lands every table in the
    default SQLite schema. Restores them in a finally block so other tests
    using the shared SQLModel.metadata aren't poisoned.
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
        with Session(engine) as s:
            yield s
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture()
def db_client(real_session, fake_embed_client, tmp_path, monkeypatch):
    """TestClient backed by a real session — used by the soft-delete tests."""
    monkeypatch.setenv(VAULT_ROOT_ENV, str(tmp_path))
    app.dependency_overrides[get_session] = lambda: real_session
    app.dependency_overrides[get_embedding_client] = lambda: fake_embed_client
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestCreateNote:
    """Tests for POST /api/knowledge/notes."""

    def test_create_note_writes_file(self, client, tmp_path):
        """POST with content+title returns 201 and writes file with frontmatter."""
        r = client.post(
            "/api/knowledge/notes",
            json={
                "content": "This is my note content.",
                "title": "My Test Note",
                "source": "manual",
                "tags": ["test", "example"],
                "type": "note",
            },
        )

        assert r.status_code == 201
        body = r.json()
        path = body.get("path", "")
        assert path.endswith(".md")

        written = (tmp_path / path).read_text()
        # Parse frontmatter (between --- delimiters)
        parts = written.split("---\n")
        assert len(parts) >= 3, f"Expected frontmatter delimiters, got: {written}"
        fm = yaml.safe_load(parts[1])
        assert fm["title"] == "My Test Note"
        assert fm["source"] == "manual"
        assert fm["tags"] == ["test", "example"]
        assert fm["type"] == "note"
        # Content follows the frontmatter
        assert "This is my note content." in parts[2]

    def test_create_note_content_required(self, client):
        """POST without content field returns 422 (Pydantic validation)."""
        r = client.post(
            "/api/knowledge/notes",
            json={"title": "No content"},
        )
        assert r.status_code == 422

    def test_create_note_empty_content_rejected(self, client):
        """POST with whitespace-only content returns 400."""
        r = client.post(
            "/api/knowledge/notes",
            json={"content": "   \n  "},
        )
        assert r.status_code == 400
        assert "content" in r.json().get("detail", "").lower()

    def test_create_note_generates_title_from_content(self, client, tmp_path):
        """POST without title uses first 60 chars of content as title."""
        content = "A short note about something interesting"
        r = client.post(
            "/api/knowledge/notes",
            json={"content": content},
        )

        assert r.status_code == 201
        path = r.json().get("path", "")
        written = (tmp_path / path).read_text()
        parts = written.split("---\n")
        fm = yaml.safe_load(parts[1])
        assert fm["title"] == content[:60]

    def test_create_note_collision_appends_suffix(self, client, tmp_path):
        """POST with title that collides gets a -1 suffix."""
        # Pre-create the file at the expected slug path
        (tmp_path / "my-note.md").write_text("existing")

        r = client.post(
            "/api/knowledge/notes",
            json={"content": "New content", "title": "My Note"},
        )

        assert r.status_code == 201
        path = r.json().get("path", "")
        assert path == "my-note-1.md"
        assert (tmp_path / path).exists()


def _insert_note(session: Session, *, note_id: str, path: str) -> Note:
    """Insert a Note row used by the soft-delete tests."""
    note = Note(
        note_id=note_id,
        path=path,
        title=note_id,
        content_hash=f"hash-{note_id}",
        type="atom",
        created_at=datetime.now(timezone.utc),
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


class TestDeleteNote:
    """Tests for DELETE /api/knowledge/notes/{note_id}.

    Behaviour changed from the original hard-delete to a soft-delete
    that moves the file into ``_trash/`` and stamps ``deleted_at`` on
    the row. The DB row survives so POST /undelete can restore it.
    """

    def test_delete_note_soft_deletes_and_moves_to_trash(
        self, db_client, real_session, tmp_path
    ):
        note_path = "delete-me.md"
        (tmp_path / note_path).write_text("---\ntitle: Doomed\n---\n\nGoodbye\n")
        note = _insert_note(real_session, note_id="del123", path=note_path)

        r = db_client.delete("/api/knowledge/notes/del123")

        assert r.status_code == 200
        body = r.json()
        assert body.get("id") == "del123"
        assert body.get("deleted_at") is not None

        # Original file is gone; trash file exists.
        assert not (tmp_path / note_path).exists()
        trash_files = list((tmp_path / "_trash").glob("*-delete-me.md"))
        assert len(trash_files) == 1, f"expected 1 trash entry, got {trash_files}"

        # DB row survives with deleted_at set and pre_delete_path captured.
        real_session.expire_all()
        reloaded = real_session.get(Note, note.id)
        assert reloaded is not None
        assert reloaded.deleted_at is not None
        assert reloaded.pre_delete_path == note_path
        assert reloaded.path.startswith("_trash/")

    def test_delete_note_not_found(self, db_client):
        """DELETE for nonexistent note_id returns 404."""
        r = db_client.delete("/api/knowledge/notes/nonexistent")
        assert r.status_code == 404
        detail = r.json().get("detail", "")
        assert "not found" in detail.lower()

    def test_delete_note_already_deleted_returns_404(
        self, db_client, real_session, tmp_path
    ):
        """A second DELETE on a soft-deleted note 404s — the row is hidden."""
        note_path = "already-gone.md"
        (tmp_path / note_path).write_text("---\ntitle: Ghost\n---\n\nBoo\n")
        _insert_note(real_session, note_id="gone456", path=note_path)

        first = db_client.delete("/api/knowledge/notes/gone456")
        assert first.status_code == 200

        second = db_client.delete("/api/knowledge/notes/gone456")
        assert second.status_code == 404

    def test_delete_note_missing_file_still_soft_deletes(
        self, db_client, real_session, tmp_path
    ):
        """DELETE when file is already gone still stamps the row."""
        # Insert a row but don't write the file — simulate external deletion.
        _insert_note(real_session, note_id="ghost", path="ghost.md")

        r = db_client.delete("/api/knowledge/notes/ghost")
        assert r.status_code == 200
        body = r.json()
        assert body.get("deleted_at") is not None
        # No _trash file expected because there was nothing to move.
        assert not (tmp_path / "_trash").exists()


class TestEditNote:
    """Tests for PUT /api/knowledge/notes/{note_id}."""

    def test_edit_note_updates_content(self, client, tmp_path):
        """PUT with new content+title returns 200 and updates the file."""
        # Create an existing vault file with frontmatter
        note_path = "my-note.md"
        original = "---\ntitle: Original Title\ntags:\n- old\n---\n\nOriginal body\n"
        (tmp_path / note_path).write_text(original)

        mock_note = {
            "note_id": "abc123",
            "title": "Original Title",
            "path": note_path,
            "type": "note",
            "tags": ["old"],
        }

        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = mock_note

            r = client.put(
                "/api/knowledge/notes/abc123",
                json={"content": "Updated body", "title": "Updated Title"},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["note_id"] == "abc123"
        assert body["path"] == note_path

        # Verify file was updated
        written = (tmp_path / note_path).read_text()
        parts = written.split("---\n")
        assert len(parts) >= 3
        fm = yaml.safe_load(parts[1])
        assert fm["title"] == "Updated Title"
        assert "Updated body" in parts[2]

    def test_edit_note_not_found(self, client):
        """PUT for nonexistent note_id returns 404."""
        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = None

            r = client.put(
                "/api/knowledge/notes/nonexistent",
                json={"content": "New content"},
            )

        assert r.status_code == 404
        assert "note not found" in r.json().get("detail", "")

    def test_edit_note_missing_vault_file(self, client, tmp_path):
        """PUT when note exists in DB but vault file is gone returns 404."""
        mock_note = {
            "note_id": "abc123",
            "title": "Ghost Note",
            "path": "gone.md",
            "type": "note",
            "tags": [],
        }

        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = mock_note

            r = client.put(
                "/api/knowledge/notes/abc123",
                json={"title": "New Title"},
            )

        assert r.status_code == 404
        assert "vault file missing" in r.json().get("detail", "")

    def test_edit_note_preserves_visibility_in_frontmatter(self, client, tmp_path):
        """PUT must preserve visibility frontmatter on body/title rewrites.

        Regression guard for the public-notes-visibility V1: the PUT endpoint
        rewrites the file's frontmatter from the parsed metadata. If a future
        refactor drops ``visibility`` from the re-serialized frontmatter, the
        next reconciler pass will null the column on disk and silently leak
        previously-public notes back to private (or vice versa). Tasks 4-5
        wire ``visibility`` through the parser -> store flow; this test makes
        sure the PUT round-trip doesn't undo that on every manual edit.
        """
        note_path = "rt.md"
        original = (
            "---\n"
            "title: Round Trip\n"
            "visibility: public\n"
            "tags:\n"
            "- demo\n"
            "---\n"
            "\n"
            "Original body\n"
        )
        (tmp_path / note_path).write_text(original)

        mock_note = {
            "note_id": "rt",
            "title": "Round Trip",
            "path": note_path,
            "type": "note",
            "tags": ["demo"],
        }

        with patch("knowledge.router.KnowledgeStore") as MockStore:
            MockStore.return_value.get_note_by_id.return_value = mock_note

            r = client.put(
                "/api/knowledge/notes/rt",
                json={"content": "Edited body."},
            )

        assert r.status_code == 200

        # Re-parse the rewritten file and assert the visibility key survived
        # the round-trip through the PUT serializer.
        written = (tmp_path / note_path).read_text()
        parts = written.split("---\n")
        assert len(parts) >= 3, f"Expected frontmatter delimiters, got: {written}"
        fm = yaml.safe_load(parts[1])
        assert fm.get("visibility") == "public", (
            f"PUT dropped visibility from frontmatter; got fm={fm!r}"
        )
        assert "Edited body." in parts[2]
