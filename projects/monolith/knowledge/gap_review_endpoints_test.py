"""Tests for the private-review-page gap endpoints (reject/verify/reopen + list mode).

Covers Task 2 of the private-review-page feature: the gap-side action
endpoints that back the audit and pending tabs on ``/private/review``.
Uses the same in-memory SQLite + ``TestClient`` pattern as
``gap_api_test.py`` — real DB, real filesystem, no business-logic mocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gaps import (
    GAPS_PIPELINE_VERSION,
    GapAnswerInvalidError,
    GapError,
    GapNotDeletedError,
    GapNotFoundError,
    GapWrongStateError,
    answer_gap,
    list_gaps_for_review,
    reject_gap,
    set_gap_class,
    undelete_gap,
)
from knowledge.models import Gap, Note


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite can't span schemas — strip Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default
    # schema. Mirror the pattern in gap_api_test.py.
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


def _make_source_note(session: Session, note_id: str = "src") -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/{note_id}.md",
        title=note_id,
        content_hash=f"hash-{note_id}",
        type="atom",
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def _make_gap(
    session: Session,
    *,
    term: str,
    state: str = "in_review",
    gap_class: str | None = "internal",
    human_verified: bool = False,
    created_at: datetime | None = None,
    resolved_at: datetime | None = None,
) -> Gap:
    gap = Gap(
        term=term,
        context="",
        gap_class=gap_class,
        state=state,
        pipeline_version=GAPS_PIPELINE_VERSION,
        human_verified=human_verified,
        created_at=created_at or datetime.now(timezone.utc),
        resolved_at=resolved_at,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


# ---------------------------------------------------------------------------
# POST /api/knowledge/gaps/{gap_id}/reject
# ---------------------------------------------------------------------------


class TestRejectGap:
    def test_happy_path_transitions_in_review_to_rejected(self, client, session):
        _make_source_note(session)
        gap = _make_gap(session, term="Bogus Term", state="in_review")

        r = client.post(f"/api/knowledge/gaps/{gap.id}/reject")

        assert r.status_code == 200
        body = r.json()
        assert body["id"] == gap.id
        assert body["state"] == "rejected"
        assert body["human_verified"] is True

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "rejected"
        assert reloaded.human_verified is True
        assert reloaded.resolved_at is not None

    def test_missing_stub_is_tolerated(self, client, session):
        """Stub absence does not break rejection — same semantics as answer_gap."""
        _make_source_note(session)
        gap = _make_gap(session, term="No Stub", state="in_review")

        r = client.post(f"/api/knowledge/gaps/{gap.id}/reject")
        assert r.status_code == 200

    def test_non_pending_state_returns_409(self, client, session):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="Already Committed",
            state="committed",
            resolved_at=datetime.now(timezone.utc),
        )

        r = client.post(f"/api/knowledge/gaps/{gap.id}/reject")
        assert r.status_code == 409
        assert "expected" in r.json().get("detail", "")

    def test_unknown_gap_id_returns_404(self, client):
        r = client.post("/api/knowledge/gaps/9999/reject")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/knowledge/gaps/{gap_id}/verify
# ---------------------------------------------------------------------------


class TestVerifyGap:
    def test_verify_pending_gap_sets_flag_without_state_change(self, client, session):
        _make_source_note(session)
        gap = _make_gap(session, term="Pending", state="in_review")

        r = client.post(f"/api/knowledge/gaps/{gap.id}/verify")
        assert r.status_code == 200
        body = r.json()
        assert body["human_verified"] is True
        assert body["state"] == "in_review"  # unchanged

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "in_review"
        assert reloaded.human_verified is True
        assert reloaded.resolved_at is None  # untouched

    def test_verify_terminal_gap_sets_flag_without_state_change(self, client, session):
        _make_source_note(session)
        resolved = datetime.now(timezone.utc) - timedelta(hours=1)
        gap = _make_gap(
            session,
            term="Auto-Committed",
            state="committed",
            resolved_at=resolved,
        )

        r = client.post(f"/api/knowledge/gaps/{gap.id}/verify")
        assert r.status_code == 200
        body = r.json()
        assert body["human_verified"] is True
        assert body["state"] == "committed"

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "committed"
        assert reloaded.human_verified is True
        # resolved_at preserved — verify doesn't touch terminal timestamps.
        assert reloaded.resolved_at is not None

    def test_verify_idempotent_on_already_verified_gap(self, client, session):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="Already Verified",
            state="in_review",
            human_verified=True,
        )

        r = client.post(f"/api/knowledge/gaps/{gap.id}/verify")
        assert r.status_code == 200
        assert r.json().get("human_verified") is True

    def test_unknown_gap_id_returns_404(self, client):
        r = client.post("/api/knowledge/gaps/9999/verify")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/knowledge/gaps/{gap_id}/reopen
# ---------------------------------------------------------------------------


class TestReopenGap:
    @pytest.mark.parametrize("state", ["committed", "rejected", "parked"])
    def test_terminal_state_reopens_to_in_review(self, client, session, state):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term=f"reopen-{state}",
            state=state,
            human_verified=True,
            resolved_at=datetime.now(timezone.utc),
        )

        r = client.post(f"/api/knowledge/gaps/{gap.id}/reopen")
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "in_review"
        assert body["human_verified"] is False
        assert body["resolved_at"] is None

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "in_review"
        assert reloaded.human_verified is False
        assert reloaded.resolved_at is None

    @pytest.mark.parametrize("state", ["discovered", "in_review", "classified"])
    def test_non_terminal_state_returns_409(self, client, session, state):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term=f"non-terminal-{state}",
            state=state,
            gap_class=None if state == "discovered" else "internal",
        )

        r = client.post(f"/api/knowledge/gaps/{gap.id}/reopen")
        assert r.status_code == 409
        assert "expected" in r.json().get("detail", "")

    def test_unknown_gap_id_returns_404(self, client):
        r = client.post("/api/knowledge/gaps/9999/reopen")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/knowledge/gaps/review-queue?mode=...
# ---------------------------------------------------------------------------


class TestReviewQueueModes:
    def test_pending_mode_default_excludes_verified(self, client, session):
        """The new ``human_verified IS FALSE`` filter — gaps that have been
        ``/verify``'d must drop out of the pending queue even though they
        remain in ``state='in_review'``."""
        _make_source_note(session)
        now = datetime.now(timezone.utc)
        _make_gap(
            session,
            term="unverified",
            state="in_review",
            gap_class="internal",
            human_verified=False,
            created_at=now - timedelta(seconds=30),
        )
        _make_gap(
            session,
            term="verified-skipped",
            state="in_review",
            gap_class="internal",
            human_verified=True,
            created_at=now - timedelta(seconds=20),
        )

        r = client.get("/api/knowledge/gaps/review-queue")
        assert r.status_code == 200
        terms = [g["term"] for g in r.json().get("gaps", [])]
        assert terms == ["unverified"]

    def test_pending_mode_explicit_same_as_default(self, client, session):
        _make_source_note(session)
        _make_gap(
            session,
            term="ping",
            state="in_review",
            gap_class="hybrid",
            human_verified=False,
        )

        default_resp = client.get("/api/knowledge/gaps/review-queue")
        explicit_resp = client.get("/api/knowledge/gaps/review-queue?mode=pending")
        assert default_resp.status_code == 200
        assert explicit_resp.status_code == 200
        assert default_resp.json() == explicit_resp.json()

    def test_audit_mode_returns_unverified_terminal_gaps(self, client, session):
        _make_source_note(session)
        now = datetime.now(timezone.utc)
        # Two terminal-state gaps, neither verified → both should appear.
        _make_gap(
            session,
            term="recent-commit",
            state="committed",
            gap_class="internal",
            human_verified=False,
            resolved_at=now - timedelta(minutes=5),
        )
        _make_gap(
            session,
            term="older-reject",
            state="rejected",
            gap_class="internal",
            human_verified=False,
            resolved_at=now - timedelta(hours=2),
        )
        # Verified terminal gap → must NOT appear.
        _make_gap(
            session,
            term="verified-park",
            state="parked",
            gap_class="parked",
            human_verified=True,
            resolved_at=now - timedelta(minutes=1),
        )
        # Pending gap → must NOT appear in audit mode (filters on terminal).
        _make_gap(
            session,
            term="still-pending",
            state="in_review",
            gap_class="internal",
            human_verified=False,
            created_at=now,
        )

        r = client.get("/api/knowledge/gaps/review-queue?mode=audit")
        assert r.status_code == 200
        terms = [g["term"] for g in r.json().get("gaps", [])]
        # Most-recently-resolved first.
        assert terms == ["recent-commit", "older-reject"]

    def test_audit_mode_includes_state_and_resolved_at(self, client, session):
        _make_source_note(session)
        now = datetime.now(timezone.utc)
        _make_gap(
            session,
            term="audit-row",
            state="committed",
            human_verified=False,
            resolved_at=now,
        )

        r = client.get("/api/knowledge/gaps/review-queue?mode=audit")
        assert r.status_code == 200
        gap = r.json().get("gaps", [])[0]
        assert gap["state"] == "committed"
        assert gap["resolved_at"] is not None
        assert gap["human_verified"] is False

    def test_invalid_mode_returns_422(self, client):
        r = client.get("/api/knowledge/gaps/review-queue?mode=junk")
        assert r.status_code == 422

    def test_pending_mode_includes_external_in_review(self, session):
        """External gaps in `in_review` must appear in pending so the user
        can approve them. Regression guard for the v2 gating cutover: before
        this test, list_gaps_for_review filtered gap_class IN (internal,
        hybrid) and the migrated backlog never rendered in the UI.
        """
        gap = _make_gap(
            session,
            term="rust-ownership",
            state="in_review",
            gap_class="external",
        )
        rows = list_gaps_for_review(session, mode="pending")
        assert any(r["id"] == gap.id for r in rows), (
            "external in_review gaps must be in the pending queue alongside "
            "internal/hybrid"
        )


# ---------------------------------------------------------------------------
# answer_gap now sets human_verified=True
# ---------------------------------------------------------------------------


class TestAnswerGapSetsHumanVerified:
    @pytest.mark.asyncio
    async def test_answer_sets_human_verified_true(self, session):
        """Direct call to answer_gap (no HTTP) must mark human_verified."""
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="answer-verifies",
            state="in_review",
            gap_class="internal",
            human_verified=False,
        )

        with patch(
            "knowledge.mcp._index_atom", AsyncMock(return_value="answer-verifies")
        ):
            await answer_gap(session, gap.id, "user-written answer")

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "committed"
        assert reloaded.human_verified is True


# ---------------------------------------------------------------------------
# DELETE / POST .../undelete — soft-delete + undo for gaps
# ---------------------------------------------------------------------------


class TestSoftDeleteGap:
    """Tests for DELETE /api/knowledge/gaps/{gap_id} and POST .../undelete.

    Soft-delete stamps ``deleted_at`` and hard-deletes the
    ``_researching/<slug>.md`` stub (regenerable). Undelete clears
    ``deleted_at``; the stub is regenerated lazily by the next
    ``discover_gaps`` cycle (not by undelete itself).
    """

    def test_delete_sets_deleted_at_and_removes_from_audit_queue(self, client, session):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="doomed-audit",
            state="committed",
            gap_class="internal",
            human_verified=False,
            resolved_at=datetime.now(timezone.utc),
        )

        # Sanity: appears in audit queue first.
        r0 = client.get("/api/knowledge/gaps/review-queue?mode=audit")
        assert gap.id in [g["id"] for g in r0.json().get("gaps", [])]

        r = client.delete(f"/api/knowledge/gaps/{gap.id}")
        assert r.status_code == 200
        body = r.json()
        assert body.get("id") == gap.id
        assert body.get("deleted_at") is not None

        # After delete: gone from audit.
        r1 = client.get("/api/knowledge/gaps/review-queue?mode=audit")
        assert gap.id not in [g["id"] for g in r1.json().get("gaps", [])]

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.deleted_at is not None

    def test_delete_removes_pending_gap_from_pending_queue(self, client, session):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="doomed-pending",
            state="in_review",
            gap_class="internal",
            human_verified=False,
        )

        r0 = client.get("/api/knowledge/gaps/review-queue")
        assert gap.id in [g["id"] for g in r0.json().get("gaps", [])]

        r = client.delete(f"/api/knowledge/gaps/{gap.id}")
        assert r.status_code == 200

        r1 = client.get("/api/knowledge/gaps/review-queue")
        assert gap.id not in [g["id"] for g in r1.json().get("gaps", [])]

    def test_delete_is_idempotent(self, client, session):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="double-delete",
            state="in_review",
            gap_class="internal",
        )

        first = client.delete(f"/api/knowledge/gaps/{gap.id}")
        assert first.status_code == 200
        first_ts = first.json().get("deleted_at")
        assert first_ts is not None

        # Second DELETE returns the same payload (idempotent), NOT a 404.
        second = client.delete(f"/api/knowledge/gaps/{gap.id}")
        assert second.status_code == 200
        assert second.json().get("deleted_at") == first_ts

    def test_get_lifecycle_endpoints_404_after_delete(self, client, session):
        """Write helpers (_get_gap_or_raise) treat deleted as not-found."""
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="hidden-gap",
            state="in_review",
            gap_class="internal",
        )

        client.delete(f"/api/knowledge/gaps/{gap.id}")

        # reject/verify/reopen all go through _get_gap_or_raise.
        r_reject = client.post(f"/api/knowledge/gaps/{gap.id}/reject")
        assert r_reject.status_code == 404

        r_verify = client.post(f"/api/knowledge/gaps/{gap.id}/verify")
        assert r_verify.status_code == 404

    def test_undelete_restores_row_to_queues(self, client, session):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="round-trip-gap",
            state="in_review",
            gap_class="internal",
        )

        client.delete(f"/api/knowledge/gaps/{gap.id}")
        r = client.post(f"/api/knowledge/gaps/{gap.id}/undelete")
        assert r.status_code == 200
        body = r.json()
        assert body.get("id") == gap.id
        assert body.get("deleted_at") is None

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.deleted_at is None

        # Back in the pending queue.
        r1 = client.get("/api/knowledge/gaps/review-queue")
        assert gap.id in [g["id"] for g in r1.json().get("gaps", [])]

    def test_undelete_live_gap_returns_409(self, client, session):
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="alive-gap",
            state="in_review",
            gap_class="internal",
        )

        r = client.post(f"/api/knowledge/gaps/{gap.id}/undelete")
        # "is not deleted" maps to 409 (distinct from 404 not-found).
        assert r.status_code == 409

    def test_undelete_unknown_gap_returns_404(self, client):
        r = client.post("/api/knowledge/gaps/9999/undelete")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Richer review-queue response dicts
# ---------------------------------------------------------------------------


class TestGapReviewQueueDictShape:
    """Tests for the audit-UI fields added in this PR.

    The original /private/review page asked the user to evaluate gaps
    from only `term`/`gap_class`/`state` — too thin to actually decide.
    The dict now includes `referenced_by_count`, `research_attempts`, and
    `answer` so the UI can render in-place.
    """

    def test_dict_includes_new_audit_fields(self, client, session):
        _make_source_note(session)
        _make_gap(
            session,
            term="rich-gap",
            state="committed",
            gap_class="internal",
            human_verified=False,
            resolved_at=datetime.now(timezone.utc),
        )

        r = client.get("/api/knowledge/gaps/review-queue?mode=audit")
        assert r.status_code == 200
        gaps = r.json().get("gaps", [])
        assert len(gaps) == 1
        item = gaps[0]
        # Subset check — forward-compatible.
        assert set(item.keys()) >= {
            "id",
            "term",
            "context",
            "gap_class",
            "state",
            "human_verified",
            "referenced_by_count",
            "research_attempts",
            "answer",
            "deleted_at",
        }
        # No inbound links so count is 0.
        assert item["referenced_by_count"] == 0

    def test_referenced_by_count_reflects_note_links(self, client, session):
        from knowledge.models import NoteLink

        src = _make_source_note(session, note_id="linker")
        _make_gap(
            session,
            term="linked-term",
            state="in_review",
            gap_class="internal",
        )
        # Two wikilinks pointing at the gap's term, plus a frontmatter
        # edge that should NOT count (kind='edge' excluded).
        session.add(
            NoteLink(
                src_note_fk=src.id,
                target_id="linked-term",
                target_title=None,
                kind="link",
                edge_type=None,
            )
        )
        session.add(
            NoteLink(
                src_note_fk=src.id,
                target_id="linked-term",
                target_title=None,
                kind="link",
                edge_type=None,
            )
        )
        session.add(
            NoteLink(
                src_note_fk=src.id,
                target_id="linked-term",
                target_title=None,
                kind="edge",
                edge_type="related",
            )
        )
        session.commit()

        r = client.get("/api/knowledge/gaps/review-queue?mode=pending")
        assert r.status_code == 200
        item = r.json().get("gaps", [])[0]
        assert item["referenced_by_count"] == 2


# ---------------------------------------------------------------------------
# Typed gap-lifecycle errors (the class -> HTTP status contract)
# ---------------------------------------------------------------------------


class TestTypedGapErrors:
    """Lock the typed-exception contract that ``_map_gap_error`` maps by class.

    The gap functions raise ``GapError`` subclasses carrying a ``status_code``;
    the router maps by class instead of matching error substrings. All subclass
    ``ValueError`` so the MCP tools' ``except ValueError`` keeps working.
    """

    def test_all_subclass_value_error(self):
        for cls in (
            GapError,
            GapNotFoundError,
            GapWrongStateError,
            GapNotDeletedError,
            GapAnswerInvalidError,
        ):
            assert issubclass(cls, ValueError)

    def test_status_codes(self):
        assert GapError.status_code == 400
        assert GapNotFoundError.status_code == 404
        assert GapWrongStateError.status_code == 409
        assert GapNotDeletedError.status_code == 409
        assert GapAnswerInvalidError.status_code == 400

    def test_unknown_gap_raises_not_found(self, session):
        with pytest.raises(GapNotFoundError):
            reject_gap(session, 999999)

    def test_wrong_state_raises_wrong_state(self, session):
        gap = _make_gap(session, term="Foo", state="rejected")
        with pytest.raises(GapWrongStateError):
            reject_gap(session, gap.id)

    def test_invalid_gap_class_raises_gap_error(self, session):
        gap = _make_gap(session, term="Bar", state="discovered", gap_class=None)
        with pytest.raises(GapError) as exc_info:
            set_gap_class(session, gap.id, "bogus")
        # The base class (400), not a more specific subclass.
        assert type(exc_info.value) is GapError

    def test_undelete_live_gap_raises_not_deleted(self, session):
        gap = _make_gap(session, term="Baz", state="in_review")
        with pytest.raises(GapNotDeletedError):
            undelete_gap(session, gap.id)

    @pytest.mark.asyncio
    async def test_answer_with_frontmatter_terminator_raises_answer_invalid(
        self, session
    ):
        gap = _make_gap(session, term="Qux", state="in_review", gap_class="internal")
        with pytest.raises(GapAnswerInvalidError):
            await answer_gap(session, gap.id, "before\n---\nafter")

    def test_map_gap_error_maps_each_class_to_its_status(self):
        from knowledge.router import _map_gap_error

        cases = [
            (GapNotFoundError("x"), 404),
            (GapWrongStateError("x"), 409),
            (GapNotDeletedError("x"), 409),
            (GapAnswerInvalidError("x"), 400),
            (GapError("x"), 400),
            (ValueError("untyped"), 400),  # non-GapError falls back to 400
        ]
        for exc, expected in cases:
            assert _map_gap_error(exc).status_code == expected
