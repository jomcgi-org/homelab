"""Unit tests for knowledge.gaps.resolve_gaps_for_note (the commit hook).

When an atom is indexed whose title/alias/note_id matches an open gap's term,
the gap should transition to ``committed`` with the new note_id, a resolved_at
timestamp, and ``human_verified=False`` -- but ONLY when its gap_class is in
the CHECK-combo legal set (external/internal/hybrid). NULL/parked-class gaps
must be left untouched (committing them violates the Postgres
``gaps_state_class_combo`` CHECK, which SQLite does not enforce).

Uses the same in-memory SQLite + schema-strip fixture as gap_lifecycle_test.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from knowledge.gaps import GAPS_PIPELINE_VERSION, resolve_gaps_for_note
from knowledge.models import Gap


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


def _make_gap(
    session: Session,
    *,
    term: str,
    state: str = "discovered",
    gap_class: str | None = "external",
) -> Gap:
    gap = Gap(
        term=term,
        context="",
        state=state,
        gap_class=gap_class,
        pipeline_version=GAPS_PIPELINE_VERSION,
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


# ---------------------------------------------------------------------------
# (a) external/discovered gap matched by title -> committed
# ---------------------------------------------------------------------------


def test_match_by_title_commits_gap(session):
    gap = _make_gap(
        session, term="Bayes' Theorem", state="discovered", gap_class="external"
    )

    committed = resolve_gaps_for_note(
        session,
        note_id="bayes-theorem",
        title="Bayes' Theorem",
        aliases=None,
    )

    assert committed == [gap.id]
    session.refresh(gap)
    assert gap.state == "committed"
    assert gap.note_id == "bayes-theorem"
    assert gap.resolved_at is not None
    assert gap.human_verified is False


# ---------------------------------------------------------------------------
# (b) match by alias (not the title) -> committed
# ---------------------------------------------------------------------------


def test_match_by_alias_commits_gap(session):
    gap = _make_gap(session, term="REST", state="discovered", gap_class="internal")

    committed = resolve_gaps_for_note(
        session,
        note_id="representational-state-transfer",
        title="Representational State Transfer",
        aliases=["REST", "ReST API"],
    )

    assert committed == [gap.id]
    session.refresh(gap)
    assert gap.state == "committed"
    assert gap.note_id == "representational-state-transfer"


# ---------------------------------------------------------------------------
# (c) gap_class NULL -> NOT committed (CHECK-combo guard)
# ---------------------------------------------------------------------------


def test_null_class_gap_not_committed(session):
    gap = _make_gap(
        session, term="Unclassified Term", state="discovered", gap_class=None
    )

    committed = resolve_gaps_for_note(
        session,
        note_id="unclassified-term",
        title="Unclassified Term",
        aliases=None,
    )

    assert committed == []
    session.refresh(gap)
    assert gap.state == "discovered"
    assert gap.note_id is None
    assert gap.resolved_at is None


# ---------------------------------------------------------------------------
# (d) gap_class 'parked' -> NOT committed (CHECK-combo guard)
# ---------------------------------------------------------------------------


def test_parked_class_gap_not_committed(session):
    # 'parked' gaps live in state='parked' (terminal) in the real model, but
    # guard against the class even if the state were somehow still open.
    gap = _make_gap(session, term="Parked Term", state="discovered", gap_class="parked")

    committed = resolve_gaps_for_note(
        session,
        note_id="parked-term",
        title="Parked Term",
        aliases=None,
    )

    assert committed == []
    session.refresh(gap)
    assert gap.state == "discovered"
    assert gap.note_id is None


# ---------------------------------------------------------------------------
# (e) already-terminal gap untouched
# ---------------------------------------------------------------------------


def test_already_committed_gap_untouched(session):
    gap = _make_gap(session, term="Done Term", state="committed", gap_class="external")
    gap.note_id = "original-note"
    session.add(gap)
    session.commit()
    session.refresh(gap)

    committed = resolve_gaps_for_note(
        session,
        note_id="done-term",
        title="Done Term",
        aliases=None,
    )

    assert committed == []
    session.refresh(gap)
    assert gap.state == "committed"
    assert gap.note_id == "original-note"


def test_rejected_gap_untouched(session):
    gap = _make_gap(
        session, term="Rejected Term", state="rejected", gap_class="internal"
    )

    committed = resolve_gaps_for_note(
        session,
        note_id="rejected-term",
        title="Rejected Term",
        aliases=None,
    )

    assert committed == []
    session.refresh(gap)
    assert gap.state == "rejected"


# ---------------------------------------------------------------------------
# (f) no match -> no change
# ---------------------------------------------------------------------------


def test_no_match_no_change(session):
    gap = _make_gap(
        session, term="Some Other Term", state="discovered", gap_class="external"
    )

    committed = resolve_gaps_for_note(
        session,
        note_id="totally-unrelated",
        title="Totally Unrelated",
        aliases=["nope", "nada"],
    )

    assert committed == []
    session.refresh(gap)
    assert gap.state == "discovered"
    assert gap.note_id is None


# ---------------------------------------------------------------------------
# soft-deleted gaps are ignored even on a term match
# ---------------------------------------------------------------------------


def test_soft_deleted_gap_not_committed(session):
    gap = _make_gap(
        session, term="Deleted Term", state="discovered", gap_class="external"
    )
    gap.deleted_at = datetime.now(timezone.utc)
    session.add(gap)
    session.commit()

    committed = resolve_gaps_for_note(
        session,
        note_id="deleted-term",
        title="Deleted Term",
        aliases=None,
    )

    assert committed == []
    session.refresh(gap)
    assert gap.state == "discovered"
