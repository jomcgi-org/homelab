"""Verify the audit-queue verification columns declare the right defaults.

These columns are the foundation of the /private/review audit surface: a
False value means "automation made this call and a human has not confirmed
it," so the row should surface in the review queue. We assert the column
declarations directly (no DB roundtrip) because the original DB-backed
fixture mutated ``SQLModel.metadata`` to strip schemas for SQLite, which
contaminated SQLAlchemy's compiled-query cache for downstream Postgres
tests in the same process.
"""

from sqlalchemy.sql import elements

from knowledge.models import Gap, Note


def test_gap_human_verified_column_defaults_to_false() -> None:
    column = Gap.__table__.columns["human_verified"]
    assert column.nullable is False
    assert column.default.arg is False
    assert isinstance(column.server_default.arg, elements.TextClause)
    assert column.server_default.arg.text == "false"


def test_note_visibility_verified_column_defaults_to_false() -> None:
    column = Note.__table__.columns["visibility_verified"]
    assert column.nullable is False
    assert column.default.arg is False
    assert isinstance(column.server_default.arg, elements.TextClause)
    assert column.server_default.arg.text == "false"
