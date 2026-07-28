"""Unit tests for knowledge notes CRUD endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from core.db import get_session
from app.main import app
from knowledge.models import Note
from knowledge.router import get_embedding_client


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
def client(fake_session, fake_embed_client):
    """TestClient with an overridden (mocked) session and embedder."""
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
def db_client(real_session, fake_embed_client):
    """TestClient backed by a real session — used by the soft-delete + edit tests."""
    app.dependency_overrides[get_session] = lambda: real_session
    app.dependency_overrides[get_embedding_client] = lambda: fake_embed_client
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestCreateNote:
    """Tests for POST /api/knowledge/notes (fileless: raw_inputs + S3, ADR 006)."""

    def test_create_note_returns_raw_id_and_calls_ingest_raw(self, client):
        """POST returns 201 with {raw_id} and inserts a raw via ingest_raw."""
        with patch("knowledge.router.ingest_raw") as mock_ingest:
            mock_ingest.return_value = MagicMock(raw_id="deadbeef")
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
        assert r.json() == {"raw_id": "deadbeef"}

        mock_ingest.assert_called_once()
        kwargs = mock_ingest.call_args.kwargs
        assert kwargs["source"] == "manual"
        content = kwargs["content"]
        # Frontmatter + body assembled from the request fields.
        parts = content.split("---\n")
        assert len(parts) >= 3, f"Expected frontmatter delimiters, got: {content}"
        fm = yaml.safe_load(parts[1])
        assert fm["title"] == "My Test Note"
        assert fm["source"] == "manual"
        assert fm["tags"] == ["test", "example"]
        assert fm["type"] == "note"
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

    def test_create_note_generates_title_from_content(self, client):
        """POST without title uses first 60 chars of content as title."""
        content = "A short note about something interesting"
        with patch("knowledge.router.ingest_raw") as mock_ingest:
            mock_ingest.return_value = MagicMock(raw_id="abc123")
            r = client.post(
                "/api/knowledge/notes",
                json={"content": content},
            )

        assert r.status_code == 201
        built = mock_ingest.call_args.kwargs["content"]
        fm = yaml.safe_load(built.split("---\n")[1])
        assert fm["title"] == content[:60]

    def test_create_note_defaults_source_to_capture(self, client):
        """POST without source defaults the raw source to 'capture'."""
        with patch("knowledge.router.ingest_raw") as mock_ingest:
            mock_ingest.return_value = MagicMock(raw_id="xyz")
            r = client.post(
                "/api/knowledge/notes",
                json={"content": "no source given"},
            )

        assert r.status_code == 201
        assert mock_ingest.call_args.kwargs["source"] == "capture"


def _insert_note(
    session: Session,
    *,
    note_id: str,
    path: str,
    title: str | None = None,
    content: str | None = None,
    tags: list[str] | None = None,
    visibility: str | None = None,
) -> Note:
    """Insert a Note row used by the soft-delete + edit tests.

    DB-only after ADR 006: ``content`` is the authoritative body, so the
    edit tests set it here rather than writing a vault file.
    """
    note = Note(
        note_id=note_id,
        path=path,
        title=title or note_id,
        content=content,
        content_hash=f"hash-{note_id}",
        type="atom",
        tags=tags or [],
        visibility=visibility,  # type: ignore[arg-type]
        created_at=datetime.now(timezone.utc),
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def _reload_note(session: Session, note_id: str) -> Note | None:
    """Fetch the current Note row by stable ``note_id`` from a fresh read.

    ``edit_note`` re-indexes by deleting and re-inserting the row (new
    primary key), so callers must look it up by ``note_id``, not the old
    ``id``. ``expire_all`` drops the stale identity-map copy first.
    """
    session.expire_all()
    # test helper: intentionally reads any row (including soft-deleted).
    stmt = select(Note).where(
        Note.note_id == note_id
    )  # nosemgrep: sqlmodel-select-missing-deleted-at-filter
    return session.exec(stmt).one_or_none()


class TestDeleteNote:
    """Tests for DELETE /api/knowledge/notes/{note_id}.

    DB-only soft-delete (ADR 006, Obsidian decommissioned): stamps
    ``deleted_at`` and captures ``pre_delete_path`` on the row. There is no
    file to move and no ``_trash/`` directory; ``path`` is unchanged. The
    DB row survives so POST /undelete can restore it.
    """

    def test_delete_note_soft_deletes_and_stamps_row(self, db_client, real_session):
        note = _insert_note(real_session, note_id="del123", path="delete-me.md")

        r = db_client.delete("/api/knowledge/notes/del123")

        assert r.status_code == 200
        body = r.json()
        assert body.get("id") == "del123"
        assert body.get("deleted_at") is not None

        # DB row survives with deleted_at set, pre_delete_path captured, and
        # path UNCHANGED (no _trash move).
        reloaded = _reload_note(real_session, "del123")
        assert reloaded is not None
        assert reloaded.id == note.id
        assert reloaded.deleted_at is not None
        assert reloaded.pre_delete_path == "delete-me.md"
        assert reloaded.path == "delete-me.md"

    def test_delete_note_not_found(self, db_client):
        """DELETE for nonexistent note_id returns 404."""
        r = db_client.delete("/api/knowledge/notes/nonexistent")
        assert r.status_code == 404
        detail = r.json().get("detail", "")
        assert "not found" in detail.lower()

    def test_delete_note_already_deleted_returns_404(self, db_client, real_session):
        """A second DELETE on a soft-deleted note 404s — the row is hidden."""
        _insert_note(real_session, note_id="gone456", path="already-gone.md")

        first = db_client.delete("/api/knowledge/notes/gone456")
        assert first.status_code == 200

        second = db_client.delete("/api/knowledge/notes/gone456")
        assert second.status_code == 404


class TestEditNote:
    """Tests for PUT /api/knowledge/notes/{note_id}.

    DB-only after ADR 006: ``edit_note`` reconstructs frontmatter from the
    authoritative Postgres row and re-indexes synchronously, so these tests
    insert a real Note row (with ``content`` set) into a real session and
    rely on the fake embedder injected by ``db_client``.
    """

    def test_edit_note_updates_content(self, db_client, real_session):
        """PUT with new content+title returns 200 and updates the DB row."""
        _insert_note(
            real_session,
            note_id="abc123",
            path="my-note.md",
            title="Original Title",
            content="Original body",
            tags=["old"],
        )

        r = db_client.put(
            "/api/knowledge/notes/abc123",
            json={"content": "Updated body", "title": "Updated Title"},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["note_id"] == "abc123"
        assert body["path"] == "my-note.md"

        # The re-indexed DB row carries the new body + title. The index
        # pipeline stores the body with surrounding frontmatter whitespace,
        # so compare on the stripped body.
        reloaded = _reload_note(real_session, "abc123")
        assert reloaded is not None
        assert reloaded.title == "Updated Title"
        assert (reloaded.content or "").strip() == "Updated body"

    def test_edit_note_not_found(self, db_client):
        """PUT for an unknown note_id returns 404 (no row in the DB)."""
        r = db_client.put(
            "/api/knowledge/notes/nonexistent",
            json={"content": "New content"},
        )

        assert r.status_code == 404
        assert "note not found" in r.json().get("detail", "")

    def test_edit_note_soft_deleted_returns_404(self, db_client, real_session):
        """PUT on a soft-deleted note 404s — the row is hidden from writes."""
        _insert_note(
            real_session,
            note_id="dead",
            path="dead.md",
            content="body",
        )
        assert db_client.delete("/api/knowledge/notes/dead").status_code == 200

        r = db_client.put(
            "/api/knowledge/notes/dead",
            json={"content": "resurrect"},
        )
        assert r.status_code == 404
        assert "note not found" in r.json().get("detail", "")

    def test_edit_note_preserves_visibility_column(self, db_client, real_session):
        """PUT must preserve the ``visibility`` column on body/title rewrites.

        Regression guard for public-notes-visibility V1: ``edit_note``
        reconstructs frontmatter from the row and re-indexes. If a future
        refactor drops ``visibility`` from the reconstruction, a manual edit
        would silently null the column and leak a previously-public note back
        to private (or vice versa). DB-only after ADR 006, so this asserts the
        ``visibility`` column directly, not a disk file.
        """
        _insert_note(
            real_session,
            note_id="rt",
            path="rt.md",
            title="Round Trip",
            content="Original body",
            tags=["demo"],
            visibility="public",
        )

        r = db_client.put(
            "/api/knowledge/notes/rt",
            json={"content": "Edited body."},
        )

        assert r.status_code == 200

        reloaded = _reload_note(real_session, "rt")
        assert reloaded is not None
        assert reloaded.visibility == "public", (
            f"PUT dropped visibility; got {reloaded.visibility!r}"
        )
        assert (reloaded.content or "").strip() == "Edited body."
