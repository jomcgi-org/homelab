"""Verify the audit-queue verification columns declare the right SQL defaults.

These columns are the foundation of the /private/review audit surface: a
False value means "automation made this call and a human has not confirmed
it," so the row should surface in the review queue.

We assert the column declarations directly (no DB roundtrip) because the
original DB-backed fixture mutated ``SQLModel.metadata.tables[*].schema``
to make schema-qualified tables work with SQLite ``create_all``. That
mutation contaminated SQLAlchemy's compiled-query cache, breaking
downstream Postgres-backed tests in the same pytest process.

Note: ``Field(default=False)`` is a SQLModel/Pydantic-level Python default
applied on instance construction; it is **not** visible on
``column.default``. Only the SQL-side ``server_default`` lives on the
Table column. We assert the latter here; the Python-level default is
exercised by every integration test that constructs a model.
"""

from sqlalchemy.sql import elements

from knowledge.models import Gap, Note


def test_gap_human_verified_column_server_default_is_false() -> None:
    column = Gap.__table__.columns["human_verified"]
    assert column.nullable is False
    assert isinstance(column.server_default.arg, elements.TextClause)
    assert column.server_default.arg.text == "false"


def test_note_visibility_verified_column_server_default_is_false() -> None:
    column = Note.__table__.columns["visibility_verified"]
    assert column.nullable is False
    assert isinstance(column.server_default.arg, elements.TextClause)
    assert column.server_default.arg.text == "false"
