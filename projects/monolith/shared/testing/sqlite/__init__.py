"""In-memory SQLite sessions for unit tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool


@contextmanager
def sqlite_session() -> Iterator[Session]:
    """Yield a session on a fresh in-memory database with every table created.

    SQLite cannot span schemas, so Postgres ``schema=`` overrides are stripped
    for ``create_all`` and restored afterwards.
    """
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
