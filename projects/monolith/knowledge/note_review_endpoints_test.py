"""Tests for the private-review-page note endpoints.

Covers Task 3 of the private-review-page feature: the note-side action
endpoints that back the audit and pending tabs on ``/private/review``.

    GET  /api/knowledge/notes/review-queue?mode=pending|audit
    POST /api/knowledge/notes/{note_id}/visibility
    POST /api/knowledge/notes/{note_id}/verify-visibility
    POST /api/knowledge/notes/{note_id}/reset-visibility

Uses the same in-memory SQLite + ``TestClient`` pattern as
``gap_review_endpoints_test.py`` — real DB, no business-logic mocks.
Everything is Postgres now (ADR 006, Obsidian decommissioned): the note
body lives in the ``knowledge.notes.content`` column, visibility lives in
the ``visibility`` column, and soft-delete is a pure ``deleted_at`` flip.
There is no vault directory and nothing is written to disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.models import Note


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
def client(session):
    from fastapi import FastAPI

    from core.db import get_session
    from knowledge.router import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_note(
    session: Session,
    *,
    note_id: str,
    title: str | None = None,
    body: str = "note body",
    visibility: str | None = None,
    visibility_verified: bool = False,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Note:
    """Insert a Note row. Body lives in the ``content`` column (ADR 006)."""
    title = title or note_id
    note = Note(
        note_id=note_id,
        path=f"{note_id}.md",
        title=title,
        content=body,
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
    def test_set_visibility_public_updates_db(self, client, session):
        note = _make_note(session, note_id="note-pub", body="public body")

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

    def test_set_visibility_private_updates_db(self, client, session):
        note = _make_note(session, note_id="note-priv", body="private body")

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

    def test_set_visibility_bad_value_returns_400(self, client, session):
        _make_note(session, note_id="note-bad")

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

    def test_set_visibility_missing_field_returns_422(self, client, session):
        _make_note(session, note_id="note-missing")

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
    def test_verify_with_visibility_set_flips_flag(self, client, session):
        note = _make_note(
            session,
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

    def test_verify_with_visibility_null_returns_409(self, client, session):
        _make_note(
            session,
            note_id="note-null",
            visibility=None,
        )

        r = client.post("/api/knowledge/notes/note-null/verify-visibility")

        assert r.status_code == 409
        assert "visibility is unset" in r.json().get("detail", "")

    def test_verify_unknown_note_returns_404(self, client):
        r = client.post("/api/knowledge/notes/nope/verify-visibility")
        assert r.status_code == 404

    def test_verify_is_db_only_and_leaves_content_visibility(self, client, session):
        """``/verify-visibility`` is a DB-only flag flip — it must not change
        the body ``content`` or the ``visibility`` value, only the verified flag."""
        note = _make_note(
            session,
            note_id="note-untouched",
            body="body stays put",
            visibility="private",
            visibility_verified=False,
        )

        r = client.post("/api/knowledge/notes/note-untouched/verify-visibility")
        assert r.status_code == 200

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.content == "body stays put"
        assert reloaded.visibility == "private"
        assert reloaded.visibility_verified is True


# ---------------------------------------------------------------------------
# POST /api/knowledge/notes/{note_id}/reset-visibility
# ---------------------------------------------------------------------------


class TestResetVisibility:
    def test_reset_clears_db(self, client, session):
        note = _make_note(
            session,
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

    def test_reset_unknown_note_returns_404(self, client):
        r = client.post("/api/knowledge/notes/nope/reset-visibility")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/knowledge/notes/review-queue
# ---------------------------------------------------------------------------


class TestReviewQueueModes:
    def test_pending_mode_default_returns_only_null_visibility(self, client, session):
        now = datetime.now(timezone.utc)
        _make_note(
            session,
            note_id="pending-a",
            visibility=None,
            created_at=now - timedelta(seconds=30),
        )
        _make_note(
            session,
            note_id="classified-b",
            visibility="public",
            visibility_verified=True,
            created_at=now - timedelta(seconds=20),
        )

        r = client.get("/api/knowledge/notes/review-queue")
        assert r.status_code == 200
        ids = [n["id"] for n in r.json().get("notes", [])]
        assert ids == ["pending-a"]

    def test_pending_mode_explicit_same_as_default(self, client, session):
        _make_note(session, note_id="pending-x", visibility=None)

        default_resp = client.get("/api/knowledge/notes/review-queue")
        explicit_resp = client.get("/api/knowledge/notes/review-queue?mode=pending")
        assert default_resp.status_code == 200
        assert explicit_resp.status_code == 200
        assert default_resp.json() == explicit_resp.json()

    def test_pending_ordered_oldest_first(self, client, session):
        now = datetime.now(timezone.utc)
        _make_note(
            session,
            note_id="younger",
            visibility=None,
            created_at=now,
        )
        _make_note(
            session,
            note_id="older",
            visibility=None,
            created_at=now - timedelta(hours=1),
        )

        r = client.get("/api/knowledge/notes/review-queue")
        assert r.status_code == 200
        ids = [n["id"] for n in r.json().get("notes", [])]
        assert ids == ["older", "younger"]

    def test_audit_mode_returns_unverified_classified_notes(self, client, session):
        now = datetime.now(timezone.utc)
        _make_note(
            session,
            note_id="audit-recent",
            visibility="public",
            visibility_verified=False,
            updated_at=now - timedelta(minutes=5),
        )
        _make_note(
            session,
            note_id="audit-older",
            visibility="private",
            visibility_verified=False,
            updated_at=now - timedelta(hours=2),
        )
        # Verified — must NOT appear.
        _make_note(
            session,
            note_id="audit-verified",
            visibility="public",
            visibility_verified=True,
            updated_at=now - timedelta(minutes=1),
        )
        # Pending (visibility=NULL) — must NOT appear in audit mode.
        _make_note(
            session,
            note_id="audit-pending",
            visibility=None,
            created_at=now,
        )

        r = client.get("/api/knowledge/notes/review-queue?mode=audit")
        assert r.status_code == 200
        ids = [n["id"] for n in r.json().get("notes", [])]
        # Most-recently-updated first.
        assert ids == ["audit-recent", "audit-older"]

    def test_audit_mode_excludes_verified_notes(self, client, session):
        _make_note(
            session,
            note_id="should-show",
            visibility="public",
            visibility_verified=False,
        )
        _make_note(
            session,
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

    def test_response_shape_includes_required_fields(self, client, session):
        # Body with >100 lines to exercise the 100-line snippet cap. The
        # snippet is read from the ``content`` column (ADR 006).
        long_body = "\n".join(f"line {i}" for i in range(200))
        _make_note(
            session,
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
        # Required fields per the audit-UI redesign — the original five
        # plus the new context-surfacing fields (tags/type/source).
        assert set(item.keys()) >= {
            "id",
            "title",
            "snippet",
            "visibility",
            "visibility_verified",
            "updated_at",
            "tags",
            "type",
            "source",
            "deleted_at",
        }
        # Full body must NOT be included — only the snippet.
        assert "body" not in item
        assert "content" not in item
        # Snippet is now capped at ~100 lines (was 200 chars).
        snippet_lines = item["snippet"].splitlines()
        assert len(snippet_lines) <= 100
        assert snippet_lines[0] == "line 0"
        # Should NOT include line 150 — past the 100-line cap.
        assert "line 150" not in item["snippet"]

    def test_pagination_via_limit(self, client, session):
        now = datetime.now(timezone.utc)
        # Insert 5 notes with monotonically increasing created_at so the
        # ordering is deterministic; request 3 and check we get 3.
        for i in range(5):
            _make_note(
                session,
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


# ---------------------------------------------------------------------------
# DELETE / POST .../undelete — soft-delete + undo
# ---------------------------------------------------------------------------


class TestSoftDeleteNote:
    """Tests for DELETE /api/knowledge/notes/{note_id} and POST .../undelete.

    Soft-delete is pure DB now (ADR 006, vault decommissioned): it stamps
    ``deleted_at`` and captures the current ``path`` in ``pre_delete_path``;
    the body in ``content`` and the ``path`` itself are untouched. Undelete
    clears both columns. Nothing moves on disk.
    """

    def test_delete_sets_deleted_at_and_removes_from_queue(self, client, session):
        note = _make_note(session, note_id="del-pending", visibility=None)

        # Sanity: appears in pending queue before delete.
        r0 = client.get("/api/knowledge/notes/review-queue")
        ids = [n["id"] for n in r0.json().get("notes", [])]
        assert "del-pending" in ids

        r = client.delete("/api/knowledge/notes/del-pending")
        assert r.status_code == 200
        body = r.json()
        assert body.get("id") == "del-pending"
        assert body.get("deleted_at") is not None

        # After delete: no longer in queue.
        r1 = client.get("/api/knowledge/notes/review-queue")
        ids_after = [n["id"] for n in r1.json().get("notes", [])]
        assert "del-pending" not in ids_after

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.deleted_at is not None
        assert reloaded.pre_delete_path is not None

    def test_delete_keeps_path_and_content(self, client, session):
        """Soft-delete is DB-only: ``path`` and ``content`` are unchanged,
        ``pre_delete_path`` mirrors the path, and there is no trash move."""
        note = _make_note(
            session,
            note_id="trash-me",
            body="contents to preserve",
            visibility=None,
        )
        original_path = note.path

        r = client.delete("/api/knowledge/notes/trash-me")
        assert r.status_code == 200

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.deleted_at is not None
        # Path is untouched; pre_delete_path captures it for a symmetric undo.
        assert reloaded.path == original_path
        assert reloaded.pre_delete_path == original_path
        # Body still lives in the content column.
        assert reloaded.content == "contents to preserve"

    def test_delete_already_deleted_returns_404(self, client, session):
        _make_note(session, note_id="twice", visibility=None)

        first = client.delete("/api/knowledge/notes/twice")
        assert first.status_code == 200

        second = client.delete("/api/knowledge/notes/twice")
        assert second.status_code == 404

    def test_get_visibility_endpoints_404_after_delete(self, client, session):
        """Write helpers (_get_note_or_raise) treat deleted as not-found."""
        _make_note(session, note_id="hidden", visibility="public")

        client.delete("/api/knowledge/notes/hidden")

        # All write helpers go through _get_note_or_raise — should 404 now.
        r_verify = client.post("/api/knowledge/notes/hidden/verify-visibility")
        assert r_verify.status_code == 404

        r_reset = client.post("/api/knowledge/notes/hidden/reset-visibility")
        assert r_reset.status_code == 404

        r_set = client.post(
            "/api/knowledge/notes/hidden/visibility",
            json={"visibility": "private"},
        )
        assert r_set.status_code == 404

    def test_undelete_restores_row(self, client, session):
        note = _make_note(
            session,
            note_id="round-trip",
            body="original body",
            visibility=None,
        )
        original_relative = note.path

        # Delete.
        client.delete("/api/knowledge/notes/round-trip")

        # Undelete.
        r = client.post("/api/knowledge/notes/round-trip/undelete")
        assert r.status_code == 200
        body = r.json()
        assert body.get("id") == "round-trip"
        assert body.get("deleted_at") is None

        session.expire_all()
        reloaded = session.get(Note, note.id)
        assert reloaded.deleted_at is None
        assert reloaded.pre_delete_path is None
        # Path unchanged across the round-trip; body still in the content column.
        assert reloaded.path == original_relative
        assert reloaded.content == "original body"

    def test_undelete_live_note_returns_404(self, client, session):
        """Calling undelete on a non-deleted row returns 404 (mapped from
        the Note-not-found ValueError)."""
        _make_note(session, note_id="alive", visibility=None)

        r = client.post("/api/knowledge/notes/alive/undelete")
        assert r.status_code == 404

    def test_undelete_unknown_note_returns_404(self, client):
        r = client.post("/api/knowledge/notes/nope/undelete")
        assert r.status_code == 404

    def test_delete_excludes_from_pending_and_audit(self, client, session):
        # Audit-mode note: visibility set, not verified.
        _make_note(
            session,
            note_id="audit-doomed",
            visibility="public",
            visibility_verified=False,
            updated_at=datetime.now(timezone.utc),
        )

        # Before delete: shows in audit.
        r0 = client.get("/api/knowledge/notes/review-queue?mode=audit")
        assert "audit-doomed" in [n["id"] for n in r0.json().get("notes", [])]

        client.delete("/api/knowledge/notes/audit-doomed")

        # After delete: gone from audit AND pending.
        r1 = client.get("/api/knowledge/notes/review-queue?mode=audit")
        assert "audit-doomed" not in [n["id"] for n in r1.json().get("notes", [])]
        r2 = client.get("/api/knowledge/notes/review-queue?mode=pending")
        assert "audit-doomed" not in [n["id"] for n in r2.json().get("notes", [])]
