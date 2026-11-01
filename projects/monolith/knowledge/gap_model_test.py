"""Unit tests for the knowledge.Gap SQLModel.

Exercises defaults, timestamp auto-population, and the term-global UNIQUE
constraint against an in-memory SQLite session.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from knowledge.models import Gap, Note


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


def _make_note(session: Session, note_id: str = "parent-note") -> Note:
    note = Note(
        note_id=note_id,
        path=f"_processed/atoms/{note_id}.md",
        title=note_id,
        content_hash="deadbeef",
        type="atom",
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def test_gap_defaults_state_and_created_at(session):
    """A newly-constructed Gap defaults to state='discovered' with a UTC created_at."""
    _make_note(session)
    gap = Gap(
        term="Quantum Coherence",
        pipeline_version="gardener@v1",
    )
    # default_factory runs at construction time — verify the UTC tzinfo here
    # (SQLite strips tzinfo on round-trip, so assert pre-persist).
    assert isinstance(gap.created_at, datetime)
    assert gap.created_at.tzinfo == timezone.utc

    session.add(gap)
    session.commit()

    loaded = session.get(Gap, gap.id)
    assert loaded is not None
    assert loaded.state == "discovered"
    assert loaded.context == ""
    assert loaded.gap_class is None
    assert loaded.answer is None
    assert loaded.classified_at is None
    assert loaded.resolved_at is None
    assert isinstance(loaded.created_at, datetime)
    assert loaded.pipeline_version == "gardener@v1"


def test_gap_with_explicit_class_and_context(session):
    """Optional fields (gap_class, context, answer) round-trip correctly."""
    _make_note(session, "source")
    gap = Gap(
        term="Linkerd mTLS",
        context="mentioned in [[Linkerd mTLS]] section of networking note",
        gap_class="internal",
        answer="Linkerd enables mTLS via sidecar proxies on port 4143.",
        pipeline_version="gardener@v1",
    )
    session.add(gap)
    session.commit()

    loaded = session.get(Gap, gap.id)
    assert loaded is not None
    assert loaded.gap_class == "internal"
    assert loaded.context.startswith("mentioned in")
    assert loaded.answer is not None


def test_gap_unique_term(session):
    """A second Gap with the same term must fail (UNIQUE(term))."""
    _make_note(session, "note-a")
    _make_note(session, "note-b")
    gap1 = Gap(
        term="Duplicate Term",
        pipeline_version="gardener@v1",
    )
    session.add(gap1)
    session.commit()

    gap2 = Gap(
        term="Duplicate Term",
        pipeline_version="gardener@v1",
    )
    session.add(gap2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_gap_note_id_is_nullable(session: Session) -> None:
    """note_id can be None (unset before reconciler links the stub)."""
    gap = Gap(term="foo", pipeline_version="gaps@v1")
    session.add(gap)
    session.commit()
    session.refresh(gap)
    assert gap.note_id is None


def test_gap_note_id_round_trips(session: Session) -> None:
    """note_id is persisted and readable after commit."""
    gap = Gap(term="foo", note_id="foo-slug", pipeline_version="gaps@v1")
    session.add(gap)
    session.commit()
    session.refresh(gap)
    assert gap.note_id == "foo-slug"


def test_gap_has_research_attempts_field_default_zero(session):
    """research_attempts defaults to 0 and is non-nullable."""
    gap = Gap(term="x", note_id="x", pipeline_version="test")
    session.add(gap)
    session.commit()

    fetched = session.execute(select(Gap).where(Gap.term == "x")).scalar_one()
    assert fetched.research_attempts == 0


def test_gap_research_attempts_increments(session):
    """research_attempts is a normal int column — bump and persist."""
    gap = Gap(term="x", note_id="x", pipeline_version="test")
    session.add(gap)
    session.commit()

    gap.research_attempts = 2
    session.commit()

    fetched = session.execute(select(Gap).where(Gap.term == "x")).scalar_one()
    assert fetched.research_attempts == 2


def test_gap_state_accepts_research_pipeline_values(session):
    """New state values from the research pipeline are not constrained at the
    SQLModel layer — accept researching/committed/parked alongside the
    existing classifier states."""
    for state in ("researching", "committed", "parked"):
        with session.begin_nested():
            gap = Gap(
                term=f"x-{state}",
                note_id=f"x-{state}",
                state=state,
                pipeline_version="test",
            )
            session.add(gap)
    session.commit()

    rows = session.execute(select(Gap)).scalars().all()
    states = {r.state for r in rows}
    assert {"researching", "committed", "parked"}.issubset(states)
