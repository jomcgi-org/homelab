"""Tests for the private-review-page gap endpoints (reject/verify/reopen + list mode).

Covers Task 2 of the private-review-page feature: the gap-side action
endpoints that back the audit and pending tabs on ``/private/review``.
Uses the same in-memory SQLite + ``TestClient`` pattern as
``gap_api_test.py`` — real DB, real filesystem, no business-logic mocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gaps import GAPS_PIPELINE_VERSION, answer_gap
from knowledge.models import Gap, Note
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

    def test_tombstones_stub_if_present(self, client, session, tmp_path):
        from knowledge.gap_stubs import RESEARCHING_DIR

        _make_source_note(session)
        gap = _make_gap(session, term="Stubbed Term", state="in_review")

        stub_dir = tmp_path / RESEARCHING_DIR
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub_path = stub_dir / "stubbed-term.md"
        stub_path.write_text("---\nid: stubbed-term\n---\n\nstub body\n")
        assert stub_path.exists()

        r = client.post(f"/api/knowledge/gaps/{gap.id}/reject")
        assert r.status_code == 200
        assert not stub_path.exists()

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


# ---------------------------------------------------------------------------
# answer_gap now sets human_verified=True
# ---------------------------------------------------------------------------


class TestAnswerGapSetsHumanVerified:
    def test_answer_sets_human_verified_true(self, session, tmp_path):
        """Direct call to answer_gap (no HTTP) must mark human_verified."""
        _make_source_note(session)
        gap = _make_gap(
            session,
            term="answer-verifies",
            state="in_review",
            gap_class="internal",
            human_verified=False,
        )

        answer_gap(session, gap.id, "user-written answer", tmp_path)

        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "committed"
        assert reloaded.human_verified is True
