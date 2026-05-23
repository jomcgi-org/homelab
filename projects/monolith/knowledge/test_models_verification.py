"""Verify the new audit-queue verification columns default to false.

These are the foundation of the /private/review audit surface: a False
value means "automation made this call and a human has not confirmed it,"
so the row should surface in the review queue. Newly-inserted rows must
default to False without the caller having to set the column explicitly,
so all pre-existing/historical rows surface for review on first deploy.
"""

import pytest
from sqlmodel import Session, SQLModel, create_engine
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


def test_gap_human_verified_defaults_false(session: Session) -> None:
    """A Gap inserted without specifying human_verified persists as False."""
    gap = Gap(term="test-term", pipeline_version="gardener@v1")
    session.add(gap)
    session.commit()
    session.refresh(gap)
    assert gap.human_verified is False


def test_note_visibility_verified_defaults_false(session: Session) -> None:
    """A Note inserted without specifying visibility_verified persists as False."""
    note = Note(
        note_id="test-note",
        path="_processed/atoms/test-note.md",
        title="Test",
        content_hash="deadbeef",
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    assert note.visibility_verified is False
