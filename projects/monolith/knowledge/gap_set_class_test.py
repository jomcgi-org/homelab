"""Unit tests for knowledge.gaps.set_gap_class.

Drives the fileless classification path: a claude.ai routine reads
discovered, NULL-class gaps and writes the decision back via set_gap_class.
Each transition must leave a (state, gap_class) pair that is legal under the
Postgres gaps_state_class_combo CHECK. SQLite create_all does NOT enforce
that CHECK, so these tests assert the legal-combo invariant explicitly.

Uses the same in-memory SQLite + schema-strip fixture pattern as
gap_lifecycle_test.py so table DDL works without a real Postgres.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from knowledge.gaps import set_gap_class
from knowledge.models import Gap
from shared.testing.sqlite import sqlite_session

# The only (state, gap_class) pairs this function may produce, mirrored from
# the gaps_state_class_combo CHECK semantics: 'discovered' is a wildcard,
# 'in_review' requires a user-review class, 'parked' requires 'parked'.
_LEGAL_COMBOS = {
    ("discovered", "external"),
    ("in_review", "internal"),
    ("in_review", "hybrid"),
    ("parked", "parked"),
}


@pytest.fixture(name="session")
def session_fixture():
    with sqlite_session() as session:
        yield session


def _make_gap(
    session: Session,
    *,
    term: str = "some-term",
    state: str = "discovered",
    gap_class: str | None = None,
) -> Gap:
    gap = Gap(
        term=term,
        context="referenced by note-a",
        note_id=term,
        state=state,
        gap_class=gap_class,
        pipeline_version="gaps@v1",
    )
    session.add(gap)
    session.commit()
    session.refresh(gap)
    return gap


def test_external_stays_discovered(session: Session) -> None:
    gap = _make_gap(session)
    result = set_gap_class(session, gap.id, "external")
    assert result["gap_class"] == "external"
    assert result["state"] == "discovered"
    assert result["resolved_at"] is None
    session.refresh(gap)
    assert gap.gap_class == "external"
    assert gap.state == "discovered"
    assert (gap.state, gap.gap_class) in _LEGAL_COMBOS


def test_internal_goes_to_in_review(session: Session) -> None:
    gap = _make_gap(session)
    result = set_gap_class(session, gap.id, "internal")
    assert result["gap_class"] == "internal"
    assert result["state"] == "in_review"
    session.refresh(gap)
    assert (gap.state, gap.gap_class) == ("in_review", "internal")
    assert (gap.state, gap.gap_class) in _LEGAL_COMBOS


def test_hybrid_goes_to_in_review(session: Session) -> None:
    gap = _make_gap(session)
    result = set_gap_class(session, gap.id, "hybrid")
    assert result["gap_class"] == "hybrid"
    assert result["state"] == "in_review"
    session.refresh(gap)
    assert (gap.state, gap.gap_class) == ("in_review", "hybrid")
    assert (gap.state, gap.gap_class) in _LEGAL_COMBOS


def test_parked_is_terminal_and_resolved(session: Session) -> None:
    gap = _make_gap(session)
    result = set_gap_class(session, gap.id, "parked")
    assert result["gap_class"] == "parked"
    assert result["state"] == "parked"
    assert result["resolved_at"] is not None
    session.refresh(gap)
    assert (gap.state, gap.gap_class) == ("parked", "parked")
    assert gap.resolved_at is not None
    assert (gap.state, gap.gap_class) in _LEGAL_COMBOS


def test_invalid_class_raises(session: Session) -> None:
    gap = _make_gap(session)
    with pytest.raises(ValueError):
        set_gap_class(session, gap.id, "bogus")
    session.refresh(gap)
    # Unchanged: still a discovered, NULL-class gap.
    assert gap.state == "discovered"
    assert gap.gap_class is None


def test_non_discovered_gap_raises(session: Session) -> None:
    gap = _make_gap(session, state="in_review", gap_class="internal")
    with pytest.raises(ValueError):
        set_gap_class(session, gap.id, "external")
    session.refresh(gap)
    # Unchanged: the in-flight gap is left exactly as it was.
    assert gap.state == "in_review"
    assert gap.gap_class == "internal"


def test_unknown_gap_id_raises(session: Session) -> None:
    with pytest.raises(ValueError):
        set_gap_class(session, 999999, "external")
