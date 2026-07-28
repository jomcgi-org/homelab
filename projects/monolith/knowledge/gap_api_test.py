"""Tests for the gap lifecycle HTTP endpoints.

Uses the same in-memory SQLite + TestClient fixture pattern as
``dead_letter_test.py`` — real DB, real filesystem, no mocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gaps import GAPS_PIPELINE_VERSION
from knowledge.models import Gap, Note


@pytest.fixture
def session():
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
    source_fk: int,
    state: str = "in_review",
    gap_class: str | None = "internal",
    created_at: datetime | None = None,
) -> Gap:
    gap = Gap(
        term=term,
        context="",
        gap_class=gap_class,
        state=state,
        pipeline_version=GAPS_PIPELINE_VERSION,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


class TestListGaps:
    """GET /api/knowledge/gaps."""

    def test_returns_all_gaps_by_default(self, client, session):
        src = _make_source_note(session)
        _make_gap(
            session, term="a", source_fk=src.id, state="discovered", gap_class=None
        )
        _make_gap(session, term="b", source_fk=src.id, state="in_review")

        r = client.get("/api/knowledge/gaps")
        assert r.status_code == 200
        body = r.json()
        terms = sorted(g["term"] for g in body["gaps"])
        assert terms == ["a", "b"]

    def test_filters_by_state(self, client, session):
        src = _make_source_note(session)
        _make_gap(
            session,
            term="discovered-one",
            source_fk=src.id,
            state="discovered",
            gap_class=None,
        )
        _make_gap(session, term="in-review-one", source_fk=src.id, state="in_review")
        _make_gap(
            session,
            term="classified-one",
            source_fk=src.id,
            state="classified",
            gap_class="external",
        )

        r = client.get("/api/knowledge/gaps?state=in_review,classified")
        assert r.status_code == 200
        terms = sorted(g["term"] for g in r.json().get("gaps", []))
        assert terms == ["classified-one", "in-review-one"]

    def test_filters_by_gap_class(self, client, session):
        src = _make_source_note(session)
        _make_gap(
            session,
            term="ext",
            source_fk=src.id,
            state="classified",
            gap_class="external",
        )
        _make_gap(
            session,
            term="intr",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )
        _make_gap(
            session, term="hyb", source_fk=src.id, state="in_review", gap_class="hybrid"
        )

        r = client.get("/api/knowledge/gaps?gap_class=internal,hybrid")
        assert r.status_code == 200
        terms = sorted(g["term"] for g in r.json().get("gaps", []))
        assert terms == ["hyb", "intr"]

    def test_limit_clamped_and_honored(self, client, session):
        src = _make_source_note(session)
        for i in range(5):
            _make_gap(
                session,
                term=f"t{i}",
                source_fk=src.id,
                state="discovered",
                gap_class=None,
            )

        r = client.get("/api/knowledge/gaps?limit=2")
        assert r.status_code == 200
        assert len(r.json().get("gaps", [])) == 2

    def test_limit_over_max_rejected(self, client):
        r = client.get("/api/knowledge/gaps?limit=10000")
        assert r.status_code == 422

    def test_list_gaps_handles_trailing_comma(self, client, session):
        """Trailing comma in state CSV must not become an empty-string filter.

        Regression: before the shared ``split_csv`` helper, ``state=in_review,``
        would pass ``[""]`` through as a filter value — which could silently
        hide all in_review gaps depending on the filter semantics.
        """
        src = _make_source_note(session)
        _make_gap(session, term="review-one", source_fk=src.id, state="in_review")
        _make_gap(
            session,
            term="discovered-one",
            source_fk=src.id,
            state="discovered",
            gap_class=None,
        )

        r = client.get("/api/knowledge/gaps?state=in_review,&limit=10")
        assert r.status_code == 200
        terms = sorted(g["term"] for g in r.json().get("gaps", []))
        assert terms == ["review-one"]


class TestReviewQueue:
    """GET /api/knowledge/gaps/review-queue."""

    def test_returns_internal_and_hybrid_in_review_only(self, client, session):
        src = _make_source_note(session)
        now = datetime.now(timezone.utc)
        _make_gap(
            session,
            term="a-internal",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
            created_at=now - timedelta(seconds=30),
        )
        _make_gap(
            session,
            term="b-hybrid",
            source_fk=src.id,
            state="in_review",
            gap_class="hybrid",
            created_at=now - timedelta(seconds=20),
        )
        _make_gap(
            session,
            term="c-external",
            source_fk=src.id,
            state="classified",
            gap_class="external",
            created_at=now - timedelta(seconds=10),
        )
        _make_gap(
            session,
            term="d-internal-discovered",
            source_fk=src.id,
            state="discovered",
            gap_class=None,
            created_at=now,
        )

        r = client.get("/api/knowledge/gaps/review-queue")
        assert r.status_code == 200
        body = r.json()
        terms = [g["term"] for g in body["gaps"]]
        # FIFO; only in_review + internal/hybrid.
        assert terms == ["a-internal", "b-hybrid"]

    def test_empty_queue_returns_empty_list(self, client):
        r = client.get("/api/knowledge/gaps/review-queue")
        assert r.status_code == 200
        assert r.json() == {"gaps": []}


class TestAnswerGap:
    """POST /api/knowledge/gaps/{gap_id}/answer."""

    def test_happy_path_commits_gap_fileless(self, client, session):
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="Linkerd mTLS",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )

        # The atom build+index is the shared fileless core; mock it so the
        # endpoint stays a unit test (no embeddings, no network) and assert
        # the gap commit + response shape.
        index = AsyncMock(return_value="linkerd-mtls")
        with patch("knowledge.mcp._index_atom", index):
            r = client.post(
                f"/api/knowledge/gaps/{gap.id}/answer",
                json={"answer": "Linkerd uses per-pod sidecars on port 4143."},
            )

        assert r.status_code == 200
        body = r.json()
        assert body == {"gap_id": gap.id, "note_id": "linkerd-mtls"}

        index.assert_awaited_once()
        kwargs = index.call_args.kwargs
        assert kwargs["title"] == "Linkerd mTLS"
        assert kwargs["visibility"] == "private"
        assert kwargs["source_tier"] == "personal"
        assert "Linkerd uses per-pod sidecars" in kwargs["body"]

        # Refresh in-session view — gap should be committed.
        session.expire_all()
        reloaded = session.get(Gap, gap.id)
        assert reloaded.state == "committed"
        assert reloaded.note_id == "linkerd-mtls"

    def test_unknown_gap_id_returns_404(self, client):
        r = client.post(
            "/api/knowledge/gaps/9999/answer",
            json={"answer": "anything"},
        )
        assert r.status_code == 404
        assert "Gap not found" in r.json().get("detail", "")

    def test_wrong_state_returns_409(self, client, session):
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="still-discovered",
            source_fk=src.id,
            state="discovered",
            gap_class=None,
        )

        r = client.post(
            f"/api/knowledge/gaps/{gap.id}/answer",
            json={"answer": "x"},
        )
        assert r.status_code == 409
        assert "expected 'in_review'" in r.json().get("detail", "")

    def test_answer_with_frontmatter_terminator_returns_400(self, client, session):
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="injectable",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )

        r = client.post(
            f"/api/knowledge/gaps/{gap.id}/answer",
            json={"answer": "foo\n---\nbar"},
        )
        assert r.status_code == 400
        assert "frontmatter terminator" in r.json().get("detail", "")

    def test_missing_answer_body_returns_422(self, client, session):
        src = _make_source_note(session)
        gap = _make_gap(
            session,
            term="x",
            source_fk=src.id,
            state="in_review",
            gap_class="internal",
        )

        r = client.post(f"/api/knowledge/gaps/{gap.id}/answer", json={})
        assert r.status_code == 422
