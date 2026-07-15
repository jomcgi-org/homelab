"""Unit tests for faas.repository: round-trip, upsert, smoke gate, delete.

Uses SQLModel.metadata.create_all (no migrations) on SQLite, mirroring
ships/models_test.py.
"""

from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from faas.models import Function
from faas.repository import (
    delete_function,
    get_function,
    get_visible_function,
    list_functions,
    mark_smoked,
    upsert_function,
)


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


def test_function_roundtrip(session):
    upsert_function(
        session,
        name="echo-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="a" * 64,
        code_uri="http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333/faas/echo-fn/a.zip",
        created_by="joe",
    )

    loaded = get_function(session, "echo-fn")
    assert loaded is not None
    assert loaded.name == "echo-fn"
    assert loaded.visibility == "private"
    assert loaded.runtime == "python312"
    assert loaded.handler == "app.handle"
    assert loaded.zip_sha256 == "a" * 64
    assert loaded.created_by == "joe"
    assert loaded.last_smoke_at is None
    assert isinstance(loaded.created_at, datetime)
    assert isinstance(loaded.updated_at, datetime)


def test_upsert_last_write_wins_no_duplicate(session):
    upsert_function(
        session,
        name="echo-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="a" * 64,
        code_uri="http://example/a.zip",
        created_by="joe",
    )
    first = get_function(session, "echo-fn")
    first_updated_at = first.updated_at

    upsert_function(
        session,
        name="echo-fn",
        visibility="public",
        runtime="python312",
        handler="app.handle2",
        zip_sha256="b" * 64,
        code_uri="http://example/b.zip",
        created_by="joe2",
    )

    rows = list_functions(session)
    assert len(rows) == 1

    updated = get_function(session, "echo-fn")
    assert updated.visibility == "public"
    assert updated.handler == "app.handle2"
    assert updated.zip_sha256 == "b" * 64
    assert updated.code_uri == "http://example/b.zip"
    assert updated.created_by == "joe2"
    assert updated.updated_at >= first_updated_at


def test_upsert_resets_smoke_gate_on_reregister(session):
    upsert_function(
        session,
        name="echo-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="a" * 64,
        code_uri="http://example/a.zip",
        created_by="joe",
    )
    mark_smoked(session, "echo-fn")
    assert get_visible_function(session, "echo-fn") is not None

    upsert_function(
        session,
        name="echo-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="b" * 64,
        code_uri="http://example/b.zip",
        created_by="joe",
    )

    assert get_visible_function(session, "echo-fn") is None
    assert get_function(session, "echo-fn").last_smoke_at is None


def test_mark_smoked_flips_visibility(session):
    upsert_function(
        session,
        name="echo-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="a" * 64,
        code_uri="http://example/a.zip",
        created_by="joe",
    )

    assert get_visible_function(session, "echo-fn") is None

    mark_smoked(session, "echo-fn")

    visible = get_visible_function(session, "echo-fn")
    assert visible is not None
    assert visible.last_smoke_at is not None


def test_mark_smoked_missing_function_is_noop(session):
    mark_smoked(session, "does-not-exist")
    assert get_function(session, "does-not-exist") is None


def test_list_functions_visible_only_filters(session):
    upsert_function(
        session,
        name="visible-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="a" * 64,
        code_uri="http://example/a.zip",
        created_by="joe",
    )
    upsert_function(
        session,
        name="hidden-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="b" * 64,
        code_uri="http://example/b.zip",
        created_by="joe",
    )
    mark_smoked(session, "visible-fn")

    all_rows = list_functions(session)
    assert {f.name for f in all_rows} == {"visible-fn", "hidden-fn"}

    visible_rows = list_functions(session, visible_only=True)
    assert {f.name for f in visible_rows} == {"visible-fn"}


def test_delete_function(session):
    upsert_function(
        session,
        name="echo-fn",
        visibility="private",
        runtime="python312",
        handler="app.handle",
        zip_sha256="a" * 64,
        code_uri="http://example/a.zip",
        created_by="joe",
    )

    assert delete_function(session, "echo-fn") is True
    assert get_function(session, "echo-fn") is None
    assert delete_function(session, "echo-fn") is False
