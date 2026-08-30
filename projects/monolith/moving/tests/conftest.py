"""Shared file-backed SQLite fixtures for moving domain tests."""

from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine

from moving.models import CollisionAck, Milestone, Role, Span, Task, Viewer

_MOVING_TABLES = [
    Task.__table__,
    Milestone.__table__,
    Span.__table__,
    Role.__table__,
    Viewer.__table__,
    CollisionAck.__table__,
]


@pytest.fixture(name="session")
def session_fixture(tmp_path) -> Iterator[Session]:
    """Create the moving tables in a hermetic file-backed SQLite database."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'moving_test.db'}",
        connect_args={"check_same_thread": False},
    )
    original_schemas = {table: table.schema for table in _MOVING_TABLES}
    for table in _MOVING_TABLES:
        table.schema = None
    try:
        SQLModel.metadata.create_all(engine, tables=_MOVING_TABLES)
        with Session(engine) as session:
            yield session
    finally:
        for table, schema in original_schemas.items():
            table.schema = schema
        engine.dispose()
