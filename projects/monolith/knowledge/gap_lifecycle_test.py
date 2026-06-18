"""Unit tests for knowledge.gaps — classify and review-queue helpers.

Fileless gap detection lives in ``gap_discover_fileless_test.py``; the
fileless answer path in ``gap_answer_fileless_test.py``; the commit hook in
``gap_commit_hook_test.py``; and ``set_gap_class`` in ``gap_set_class_test.py``.
This file covers the surviving legacy ``classify_gaps`` helper (injected
classifier) and ``list_review_queue``. Uses the same in-memory SQLite +
schema-strip fixture as ``gap_model_test.py`` so table DDL works without a
real Postgres.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gaps import (
    GAPS_PIPELINE_VERSION,
    classify_gaps,
    discover_gaps,
    list_review_queue,
)
from knowledge.models import Gap, Note, NoteLink


@pytest.fixture(name="session")
def session_fixture():
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
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _make_note(
    session: Session,
    note_id: str,
    *,
    title: str | None = None,
) -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/{note_id}.md",
        title=title or note_id,
        content_hash=f"hash-{note_id}",
        type="atom",
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def _add_body_link(session: Session, *, src_fk: int, target_id: str) -> None:
    session.add(
        NoteLink(
            src_note_fk=src_fk,
            target_id=target_id,
            target_title=target_id,
            kind="link",
            edge_type=None,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# classify_gaps (legacy in-pod helper; injected classifier)
# ---------------------------------------------------------------------------


def test_classify_gaps_without_classifier_is_noop(session, caplog):
    """No classifier wired → gaps stay at discovered, warning logged.

    Routing unclassified gaps to internal would conflate classifier absence
    with classifier uncertainty. The review queue must only populate once
    a real classifier lands.
    """
    src = _make_note(session, "s", title="S")
    _add_body_link(session, src_fk=src.id, target_id="t1")
    _add_body_link(session, src_fk=src.id, target_id="t2")
    discover_gaps(session)

    with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
        classified = classify_gaps(session)  # no classifier wired

    assert classified == 0
    for gap in (
        session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
    ):
        assert gap.gap_class is None
        assert gap.state == "discovered"
        assert gap.classified_at is None

    assert any(
        "2 gaps awaiting classification but no classifier is wired"
        in record.getMessage()
        for record in caplog.records
    )


def test_classify_gaps_routes_by_class(session):
    src = _make_note(session, "s", title="S")
    for target in ("ext", "int", "hyb", "park"):
        _add_body_link(session, src_fk=src.id, target_id=target)
    discover_gaps(session)

    mapping = {
        "ext": "external",
        "int": "internal",
        "hyb": "hybrid",
        "park": "parked",
    }

    def classifier(term: str, _context: str) -> str:
        return mapping[term]

    assert classify_gaps(session, classifier=classifier) == 4

    rows = {
        g.term: (g.gap_class, g.state)
        for g in session.execute(select(Gap).where(Gap.deleted_at.is_(None)))
        .scalars()
        .all()
    }
    assert rows["ext"] == ("external", "in_review")
    assert rows["int"] == ("internal", "in_review")
    assert rows["hyb"] == ("hybrid", "in_review")
    assert rows["park"] == ("parked", "classified")


def test_classify_gaps_skips_already_classified(session):
    src = _make_note(session, "s", title="S")
    _add_body_link(session, src_fk=src.id, target_id="x")
    discover_gaps(session)

    def classifier(_term: str, _context: str) -> str:
        return "internal"

    assert classify_gaps(session, classifier=classifier) == 1
    # Second call finds nothing in state='discovered'.
    assert classify_gaps(session, classifier=classifier) == 0


def test_classify_gaps_rejects_invalid_classifier_output(session, caplog):
    """Out-of-range classifier outputs fall back to internal (I2 regression)."""
    src = _make_note(session, "s", title="S")
    _add_body_link(session, src_fk=src.id, target_id="good")
    _add_body_link(session, src_fk=src.id, target_id="bad")
    discover_gaps(session)

    def classifier(term: str, _context: str) -> str:
        return "external" if term == "good" else "bogus"

    with caplog.at_level(logging.WARNING, logger="knowledge.gaps"):
        assert classify_gaps(session, classifier=classifier) == 2

    rows = {
        g.term: (g.gap_class, g.state)
        for g in session.execute(select(Gap).where(Gap.deleted_at.is_(None)))
        .scalars()
        .all()
    }
    assert rows["good"] == ("external", "in_review")
    assert rows["bad"] == ("internal", "in_review")
    assert any(
        "classifier returned invalid class 'bogus'" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# list_review_queue
# ---------------------------------------------------------------------------


def test_list_review_queue_returns_internal_hybrid_external_in_review(session):
    """All three user-actionable classes (internal/hybrid/external) at
    state=in_review must appear in the queue, FIFO by created_at. The
    queue's filter is class IN (internal, hybrid, external) AND
    state=in_review AND human_verified IS FALSE.

    Also asserts:
    - classified rows are excluded regardless of class (only in_review
      rows appear; classified is a downstream / parked state)
    - discovered rows are excluded (not yet classified)
    """
    _make_note(session, "s", title="S")
    # Manually construct gaps in varied states so we can assert filtering.
    now = datetime.now(timezone.utc)
    gaps = [
        Gap(
            term="a-internal",
            context="",
            gap_class="internal",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now - timedelta(seconds=40),
        ),
        Gap(
            term="b-hybrid",
            context="",
            gap_class="hybrid",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now - timedelta(seconds=30),
        ),
        Gap(
            term="c-external",
            context="",
            gap_class="external",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now - timedelta(seconds=20),
        ),
        Gap(
            term="d-external-classified",
            context="",
            gap_class="external",
            state="classified",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now - timedelta(seconds=10),
        ),
        Gap(
            term="e-internal-discovered",
            context="",
            gap_class="internal",
            state="discovered",
            pipeline_version=GAPS_PIPELINE_VERSION,
            created_at=now,
        ),
    ]
    session.add_all(gaps)
    session.commit()

    queue = list_review_queue(session)

    terms = [row["term"] for row in queue]
    # FIFO by created_at: internal, hybrid, external (the three in_review rows).
    # d-external-classified and e-internal-discovered are excluded by state.
    assert terms == ["a-internal", "b-hybrid", "c-external"]
    assert queue[0]["gap_class"] == "internal"
    assert queue[1]["gap_class"] == "hybrid"
    assert queue[2]["gap_class"] == "external"


def test_list_review_queue_empty(session):
    assert list_review_queue(session) == []
