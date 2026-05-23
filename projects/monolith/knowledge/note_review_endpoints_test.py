"""Tests for the private-review-page note endpoints.

Covers Task 3 of the private-review-page feature: the note-side action
endpoints that back the audit and pending tabs on ``/private/review``.

    GET  /api/knowledge/notes/review-queue?mode=pending|audit
    POST /api/knowledge/notes/{note_id}/visibility
    POST /api/knowledge/notes/{note_id}/verify-visibility
    POST /api/knowledge/notes/{note_id}/reset-visibility

Uses the same in-memory SQLite + ``TestClient`` pattern as
``gap_review_endpoints_test.py`` — real DB, real filesystem, no
business-logic mocks. Each test writes a real markdown file into the
vault temp dir so the frontmatter-write path can be verified end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.models import Note
from knowledge.service import VAULT_ROOT_ENV


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite can't span schemas — strip Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default
    # schema. Mirror the pattern in gap_review_endpoints_test.py.
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


@pytest.fixture
def client(session, tmp_path, monkeypatch):
    from fastapi import FastAPI

    from app.db import get_session
    from knowledge.router import router

    monkeypatch.setenv(VAULT_ROOT_ENV, str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _write_note_file(
    vault_root, *, note_id: str, title: str, body: str, visibility: str | None = None
) -> str:
    """Create a markdown file in ``vault_root`` and return its relative path."""
    fm: dict = {"id": note_id, "title": title}
    if visibility is not None:
        fm["visibility"] = visibility
    fm_str = yaml.dump(fm, default_flow_style=False, sort_keys=False)
    rel_path = f"{note_id}.md"
    (vault_root / rel_path).write_text(f"---\n{fm_str}---\n\n{body}\n")
    return rel_path


def _make_note(
    session: Session,
    tmp_path,
    *,
    note_id: str,
    title: str | None = None,
    body: str = "note body",
    visibility: str | None = None,
    visibility_verified: bool = False,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Note:
    title = title or note_id
    rel_path = _write_note_file(
        tmp_path,
        note_id=note_id,
        title=title,
        body=body,
        visibility=visibility,
    )
    note = Note(
        note_id=note_id,
        path=rel_path,
        title=title,
        content_hash=f"hash-{note_id}",
        type="atom",
        visibility=visibility,  # type: ignore[arg-type]
        visibility_verified=visibility_verified,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=updated_at,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


# ---------------------------------------------------------------------------
# POST /api/knowledge/notes/{note_id}/visibility
# ---------------------------------------------------------------------------


class TestSetVisibility:
    def test_set_visibility_public_updates_db_and_frontmatter(
        self, client, session, tmp_path
    ):
        note = _make_note(session, tmp_path, note_id="note-pub", body="public body")

        r = client.post(
            "/api/knowledge/notes/note-pub/visibility",
            json={"visibility": "public"},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "note-pub"
        assert body["visibility"] == "public"
        assert body["visibility_verified"] is True

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.visibility == "public"
        assert reloaded.visibility_verified is True

        # Verify the frontmatter on disk was actually rewritten.
        disk_raw = (tmp_path / note.path).read_text()
        assert "visibility: public" in disk_raw

    def test_set_visibility_private_updates_db_and_frontmatter(
        self, client, session, tmp_path
    ):
        note = _make_note(session, tmp_path, note_id="note-priv", body="private body")

        r = client.post(
            "/api/knowledge/notes/note-priv/visibility",
            json={"visibility": "private"},
        )

        assert r.status_code == 200
        body = r.json()
        assert body["visibility"] == "private"
        assert body["visibility_verified"] is True

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.visibility == "private"
        assert reloaded.visibility_verified is True

        disk_raw = (tmp_path / note.path).read_text()
        assert "visibility: private" in disk_raw

    def test_set_visibility_bad_value_returns_400(self, client, session, tmp_path):
        _make_note(session, tmp_path, note_id="note-bad")

        r = client.post(
            "/api/knowledge/notes/note-bad/visibility",
            json={"visibility": "rainbow"},
        )

        assert r.status_code == 400
        assert "visibility must be" in r.json().get("detail", "")

    def test_set_visibility_unknown_note_returns_404(self, client):
        r = client.post(
            "/api/knowledge/notes/nope/visibility",
            json={"visibility": "public"},
        )
        assert r.status_code == 404

    def test_set_visibility_missing_field_returns_422(self, client, session, tmp_path):
        _make_note(session, tmp_path, note_id="note-missing")

        # SetNoteVisibilityRequest declares visibility as required;
        # FastAPI/Pydantic surfaces missing-field errors as 422.
        r = client.post(
            "/api/knowledge/notes/note-missing/visibility",
            json={},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/knowledge/notes/{note_id}/verify-visibility
# ---------------------------------------------------------------------------


class TestVerifyVisibility:
    def test_verify_with_visibility_set_flips_flag(self, client, session, tmp_path):
        note = _make_note(
            session,
            tmp_path,
            note_id="note-v",
            visibility="public",
            visibility_verified=False,
        )

        r = client.post("/api/knowledge/notes/note-v/verify-visibility")

        assert r.status_code == 200
        body = r.json()
        assert body["visibility"] == "public"
        assert body["visibility_verified"] is True

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.visibility == "public"
        assert reloaded.visibility_verified is True

    def test_verify_with_visibility_null_returns_409(self, client, session, tmp_path):
        _make_note(
            session,
            tmp_path,
            note_id="note-null",
            visibility=None,
        )

        r = client.post("/api/knowledge/notes/note-null/verify-visibility")

        assert r.status_code == 409
        assert "visibility is unset" in r.json().get("detail", "")

    def test_verify_unknown_note_returns_404(self, client):
        r = client.post("/api/knowledge/notes/nope/verify-visibility")
        assert r.status_code == 404

    def test_verify_does_not_touch_frontmatter(self, client, session, tmp_path):
        """``/verify-visibility`` is a DB-only flag flip — no disk write."""
        note = _make_note(
            session,
            tmp_path,
            note_id="note-untouched",
            visibility="private",
        )
        before = (tmp_path / note.path).read_text()

        r = client.post("/api/knowledge/notes/note-untouched/verify-visibility")
        assert r.status_code == 200

        after = (tmp_path / note.path).read_text()
        assert before == after


# ---------------------------------------------------------------------------
# POST /api/knowledge/notes/{note_id}/reset-visibility
# ---------------------------------------------------------------------------


class TestResetVisibility:
    def test_reset_clears_db_and_frontmatter(self, client, session, tmp_path):
        note = _make_note(
            session,
            tmp_path,
            note_id="note-reset",
            visibility="public",
            visibility_verified=True,
        )

        r = client.post("/api/knowledge/notes/note-reset/reset-visibility")

        assert r.status_code == 200
        body = r.json()
        assert body["visibility"] is None
        assert body["visibility_verified"] is False

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.visibility is None
        assert reloaded.visibility_verified is False

        disk_raw = (tmp_path / note.path).read_text()
        assert "visibility:" not in disk_raw

    def test_reset_unknown_note_returns_404(self, client):
        r = client.post("/api/knowledge/notes/nope/reset-visibility")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/knowledge/notes/review-queue
# ---------------------------------------------------------------------------


class TestReviewQueueModes:
    def test_pending_mode_default_returns_only_null_visibility(
        self, client, session, tmp_path
    ):
        now = datetime.now(timezone.utc)
        _make_note(
            session,
            tmp_path,
            note_id="pending-a",
            visibility=None,
            created_at=now - timedelta(seconds=30),
        )
        _make_note(
            session,
            tmp_path,
            note_id="classified-b",
            visibility="public",
            visibility_verified=True,
            created_at=now - timedelta(seconds=20),
        )

        r = client.get("/api/knowledge/notes/review-queue")
        assert r.status_code == 200
        ids = [n["id"] for n in r.json().get("notes", [])]
        assert ids == ["pending-a"]

    def test_pending_mode_explicit_same_as_default(self, client, session, tmp_path):
        _make_note(session, tmp_path, note_id="pending-x", visibility=None)

        default_resp = client.get("/api/knowledge/notes/review-queue")
        explicit_resp = client.get("/api/knowledge/notes/review-queue?mode=pending")
        assert default_resp.status_code == 200
        assert explicit_resp.status_code == 200
        assert default_resp.json() == explicit_resp.json()

    def test_pending_ordered_oldest_first(self, client, session, tmp_path):
        now = datetime.now(timezone.utc)
        _make_note(
            session,
            tmp_path,
            note_id="younger",
            visibility=None,
            created_at=now,
        )
        _make_note(
            session,
            tmp_path,
            note_id="older",
            visibility=None,
            created_at=now - timedelta(hours=1),
        )

        r = client.get("/api/knowledge/notes/review-queue")
        assert r.status_code == 200
        ids = [n["id"] for n in r.json().get("notes", [])]
        assert ids == ["older", "younger"]

    def test_audit_mode_returns_unverified_classified_notes(
        self, client, session, tmp_path
    ):
        now = datetime.now(timezone.utc)
        _make_note(
            session,
            tmp_path,
            note_id="audit-recent",
            visibility="public",
            visibility_verified=False,
            updated_at=now - timedelta(minutes=5),
        )
        _make_note(
            session,
            tmp_path,
            note_id="audit-older",
            visibility="private",
            visibility_verified=False,
            updated_at=now - timedelta(hours=2),
        )
        # Verified — must NOT appear.
        _make_note(
            session,
            tmp_path,
            note_id="audit-verified",
            visibility="public",
            visibility_verified=True,
            updated_at=now - timedelta(minutes=1),
        )
        # Pending (visibility=NULL) — must NOT appear in audit mode.
        _make_note(
            session,
            tmp_path,
            note_id="audit-pending",
            visibility=None,
            created_at=now,
        )

        r = client.get("/api/knowledge/notes/review-queue?mode=audit")
        assert r.status_code == 200
        ids = [n["id"] for n in r.json().get("notes", [])]
        # Most-recently-updated first.
        assert ids == ["audit-recent", "audit-older"]

    def test_audit_mode_excludes_verified_notes(self, client, session, tmp_path):
        _make_note(
            session,
            tmp_path,
            note_id="should-show",
            visibility="public",
            visibility_verified=False,
        )
        _make_note(
            session,
            tmp_path,
            note_id="should-hide",
            visibility="public",
            visibility_verified=True,
        )

        r = client.get("/api/knowledge/notes/review-queue?mode=audit")
        assert r.status_code == 200
        ids = [n["id"] for n in r.json().get("notes", [])]
        assert ids == ["should-show"]
        assert "should-hide" not in ids

    def test_invalid_mode_returns_422(self, client):
        r = client.get("/api/knowledge/notes/review-queue?mode=junk")
        assert r.status_code == 422

    def test_response_shape_includes_required_fields(self, client, session, tmp_path):
        long_body = "First sentence of the body. " * 30  # > 200 chars
        _make_note(
            session,
            tmp_path,
            note_id="shape-test",
            title="Shape Test Note",
            body=long_body,
            visibility=None,
        )

        r = client.get("/api/knowledge/notes/review-queue")
        assert r.status_code == 200
        items = r.json().get("notes", [])
        assert len(items) == 1
        item = items[0]
        # Required fields per the design.
        assert set(item.keys()) >= {
            "id",
            "title",
            "snippet",
            "visibility",
            "visibility_verified",
            "updated_at",
        }
        # Full body must NOT be included — only the snippet.
        assert "body" not in item
        assert "content" not in item
        # Snippet capped at 200 chars.
        assert len(item["snippet"]) <= 200
        assert item["snippet"].startswith("First sentence")

    def test_pagination_via_limit(self, client, session, tmp_path):
        now = datetime.now(timezone.utc)
        # Insert 5 notes with monotonically increasing created_at so the
        # ordering is deterministic; request 3 and check we get 3.
        for i in range(5):
            _make_note(
                session,
                tmp_path,
                note_id=f"pg-{i}",
                visibility=None,
                created_at=now - timedelta(minutes=5 - i),
            )

        r = client.get("/api/knowledge/notes/review-queue?limit=3")
        assert r.status_code == 200
        items = r.json().get("notes", [])
        assert len(items) == 3
        # Oldest first — pg-0 was created longest ago.
        ids = [n["id"] for n in items]
        assert ids == ["pg-0", "pg-1", "pg-2"]
