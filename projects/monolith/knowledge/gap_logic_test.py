"""Unit tests for knowledge.gaps split_csv and edge-case coverage.

Gap lifecycle happy-path tests live in ``gap_lifecycle_test.py`` and
fileless detection in ``gap_discover_fileless_test.py`` (both registered
separately in BUILD). This file fills the remaining coverage:

* ``split_csv`` — all paths including None / empty / whitespace edge cases
* ``discover_gaps`` slug-folding + type='gap' exclusion edge cases
* ``list_review_queue`` return-shape / filtering guarantees

Fixture style mirrors ``gap_lifecycle_test.py``: in-memory SQLite with
schema-strip (no real Postgres needed).
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.gaps import (
    GAPS_PIPELINE_VERSION,
    discover_gaps,
    list_review_queue,
    split_csv,
)
from knowledge.models import Gap, Note, NoteLink


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session; strips Postgres schema names so DDL works."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
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
    rel_path: str | None = None,
) -> Note:
    note = Note(
        note_id=note_id,
        path=rel_path or f"_processed/{note_id}.md",
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
# split_csv
# ---------------------------------------------------------------------------


class TestSplitCsv:
    """Full coverage of split_csv() including all documented edge cases."""

    def test_returns_none_for_none_input(self):
        assert split_csv(None) is None

    def test_returns_none_for_empty_string(self):
        assert split_csv("") is None

    def test_returns_none_for_whitespace_only(self):
        assert split_csv("   ") is None

    def test_returns_none_for_all_empty_segments(self):
        """Comma-separated string where every segment is blank -> None."""
        assert split_csv(",,, ,  ,") is None

    def test_single_value(self):
        assert split_csv("discovered") == ["discovered"]

    def test_multiple_values(self):
        assert split_csv("in_review,classified") == ["in_review", "classified"]

    def test_three_values(self):
        assert split_csv("external,internal,hybrid") == [
            "external",
            "internal",
            "hybrid",
        ]

    def test_strips_leading_and_trailing_whitespace_from_segments(self):
        assert split_csv(" in_review , classified ") == ["in_review", "classified"]

    def test_drops_empty_segments_between_commas(self):
        """Two commas adjacent produce an empty segment that is dropped."""
        assert split_csv("external,,internal") == ["external", "internal"]

    def test_strips_whitespace_and_drops_empty_together(self):
        assert split_csv(" external ,  , internal ") == ["external", "internal"]

    def test_single_value_with_surrounding_whitespace(self):
        assert split_csv("  parked  ") == ["parked"]

    def test_returns_list_not_none_when_at_least_one_value(self):
        """Explicit None-vs-list check: a non-empty result is a list, not None."""
        result = split_csv("x")
        assert result is not None
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# discover_gaps - slug folding edge cases
# ---------------------------------------------------------------------------


class TestDiscoverGapsSlugFolding:
    """Two distinct terms that hash to the same slug collapse into one Gap row."""

    def test_two_terms_same_slug_produce_one_gap(self, session):
        """e.g. 'Outside-In TDD' and 'Outside In TDD' both slug to 'outside-in-tdd'."""
        src_a = _make_note(session, "src-a", title="Source A")
        src_b = _make_note(session, "src-b", title="Source B")
        _add_body_link(session, src_fk=src_a.id, target_id="Outside-In TDD")
        _add_body_link(session, src_fk=src_b.id, target_id="Outside In TDD")

        discover_gaps(session)

        rows = (
            session.execute(
                select(Gap).where(
                    Gap.note_id == "outside-in-tdd", Gap.deleted_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, (
            f"Expected 1 Gap, got {len(rows)}: {[r.term for r in rows]}"
        )

    def test_gap_notes_excluded_from_resolved_note_ids(self, session):
        """Notes of type='gap' (legacy stubs indexed as notes) are excluded
        from existing_note_ids so wikilinks pointing at such a slug are still
        seen as unresolved."""
        stub_note = Note(
            note_id="stub-slug",
            path="_researching/stub-slug.md",
            title="stub-slug",
            content_hash="stub-hash",
            type="gap",
        )
        session.add(stub_note)
        session.commit()

        src = _make_note(session, "src", title="Src")
        _add_body_link(session, src_fk=src.id, target_id="stub-slug")

        count = discover_gaps(session)

        # The wikilink pointing at the gap-typed note is NOT resolved — a new
        # Gap row must be inserted.
        assert count == 1
        rows = (
            session.execute(select(Gap).where(Gap.deleted_at.is_(None))).scalars().all()
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# list_review_queue - edge cases
# ---------------------------------------------------------------------------


class TestListReviewQueueEdgeCases:
    """Return-shape and filtering guarantees for list_review_queue."""

    def test_result_dicts_contain_required_keys(self, session):
        """Each element must contain at least id, term, context, gap_class, created_at.

        Extra keys (state, resolved_at, human_verified) are permitted; the
        review UI consumes them in audit mode. Use subset semantics so
        adding response fields later doesn't break this assertion.
        """
        gap = Gap(
            term="test-term",
            context="some context",
            gap_class="internal",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
        )
        session.add(gap)
        session.commit()

        queue = list_review_queue(session)

        assert len(queue) == 1
        item = queue[0]
        required = {"id", "term", "context", "gap_class", "created_at"}
        assert required.issubset(item.keys()), (
            f"missing required keys: {required - item.keys()}"
        )
        assert item["term"] == "test-term"
        assert item["context"] == "some context"
        assert item["gap_class"] == "internal"

    def test_includes_external_in_review_gaps(self, session):
        """state=in_review + gap_class=external appears in the queue.

        External gaps normally stay 'discovered' for the research routine, but
        the queue filter still admits in_review external rows (e.g. a manually
        reopened gap) alongside internal/hybrid.
        """
        gap = Gap(
            term="ext",
            context="",
            gap_class="external",
            state="in_review",
            pipeline_version=GAPS_PIPELINE_VERSION,
        )
        session.add(gap)
        session.commit()

        queue = list_review_queue(session)
        assert [row["term"] for row in queue] == ["ext"]
        assert queue[0]["gap_class"] == "external"
