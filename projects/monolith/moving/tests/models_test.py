"""Schema-shape and SQLite constraint tests for moving models."""

from __future__ import annotations

import re
from datetime import date, timezone
from pathlib import Path

import pytest
from sqlalchemy import Numeric, Text, UniqueConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from moving.models import (
    CollisionAck,
    GcalTombstone,
    Milestone,
    Role,
    Span,
    Task,
    Viewer,
)

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "chart/migrations"
_MIGRATION = _MIGRATIONS_DIR / "20260816000000_moving_schema.sql"
_WRITE_MIGRATION = _MIGRATIONS_DIR / "20260830000000_moving_write_surface.sql"
_GCAL_MIGRATION = _MIGRATIONS_DIR / "20260830030000_moving_gcal_tombstones.sql"
_CREATE_TABLE = re.compile(
    r"CREATE TABLE moving\.(?P<name>[a-z_][a-z0-9_]*)\s*"
    r"\((?P<body>.*?)\n\);",
    re.DOTALL,
)
_SPANS_KIND_ALTER = re.compile(
    r"ADD CONSTRAINT spans_kind_check\s+CHECK\s*\((?P<expr>kind IN \([^)]*\))\)"
)
_MODELS = {
    "tasks": Task,
    "milestones": Milestone,
    "spans": Span,
    "roles": Role,
    "viewers": Viewer,
    "collision_acks": CollisionAck,
    "gcal_tombstones": GcalTombstone,
}


def _definitions(body: str) -> list[str]:
    """Split a CREATE TABLE body on its top-level commas."""
    definitions: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(body):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            definitions.append(body[start:index].strip())
            start = index + 1
    definitions.append(body[start:].strip())
    return definitions


def _migration_tables() -> dict[str, list[str]]:
    tables = {
        match.group("name"): _definitions(match.group("body"))
        for match in _CREATE_TABLE.finditer(_MIGRATION.read_text())
    }
    surface = _WRITE_MIGRATION.read_text()
    # Guard the DROP half too: dropping a misnamed constraint would pass the
    # mirror comparison below and then fail at Atlas apply time.
    assert "DROP CONSTRAINT spans_kind_check" in surface
    tables.update(
        {
            match.group("name"): _definitions(match.group("body"))
            for match in _CREATE_TABLE.finditer(surface)
        }
    )
    tombstones = _GCAL_MIGRATION.read_text()
    tables.update(
        {
            match.group("name"): _definitions(match.group("body"))
            for match in _CREATE_TABLE.finditer(tombstones)
        }
    )
    # The write-surface migration replaces the span kind vocabulary in place.
    replacement = " ".join(_SPANS_KIND_ALTER.search(surface).group("expr").split())
    tables["spans"] = [
        re.sub(r"kind IN \([^)]*\)", replacement, definition)
        for definition in tables["spans"]
    ]
    return tables


def _sql_columns(definitions: list[str]) -> dict[str, str]:
    columns: dict[str, str] = {}
    for definition in definitions:
        match = re.match(r"(?P<name>[a-z_][a-z0-9_]*)\s+", definition)
        if match and match.group("name").upper() not in {
            "CHECK",
            "CONSTRAINT",
            "FOREIGN",
            "PRIMARY",
            "UNIQUE",
        }:
            columns[match.group("name")] = definition
    return columns


def _check_expressions(definition: str) -> list[str]:
    checks: list[str] = []
    for match in re.finditer(r"\bCHECK\s*\(", definition, re.IGNORECASE):
        start = match.end()
        depth = 1
        for index in range(start, len(definition)):
            if definition[index] == "(":
                depth += 1
            elif definition[index] == ")":
                depth -= 1
                if depth == 0:
                    checks.append(" ".join(definition[start:index].split()))
                    break
    return checks


def _sql_checks(definitions: list[str]) -> set[str]:
    return {
        expression
        for definition in definitions
        for expression in _check_expressions(definition)
    }


def _model_checks(model: type) -> set[str]:
    checks = {
        " ".join(str(constraint.sqltext).split())
        for constraint in model.__table__.constraints
        if hasattr(constraint, "sqltext")
    }
    for column in model.__table__.columns:
        checks.update(
            " ".join(str(constraint.sqltext).split())
            for constraint in column.constraints
        )
    return checks


def _sql_unique_columns(definitions: list[str]) -> set[str]:
    columns = _sql_columns(definitions)
    unique = {
        name
        for name, definition in columns.items()
        if re.search(r"\bUNIQUE\b", definition, re.IGNORECASE)
    }
    for definition in definitions:
        match = re.match(r"UNIQUE\s*\((?P<columns>[^)]+)\)", definition)
        if match:
            unique.update(name.strip() for name in match.group("columns").split(","))
    return unique


def _model_unique_columns(model: type) -> set[str]:
    return {
        column.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        for column in constraint.columns
    }


def _sql_default(definition: str) -> str | None:
    match = re.search(
        r"\bDEFAULT\s+(?P<value>'(?:''|[^'])*'|[a-z_][a-z0-9_]*\(\))",
        definition,
        re.IGNORECASE,
    )
    return match.group("value") if match else None


def test_migration_and_models_have_same_tables_and_columns():
    migration = _migration_tables()
    assert set(migration) == set(_MODELS)
    for table_name, model in _MODELS.items():
        assert model.__tablename__ == table_name
        table_args = model.__table_args__
        if isinstance(table_args, tuple):
            table_args = table_args[-1]
        assert table_args == {
            "schema": "moving",
            "extend_existing": True,
        }
        assert list(_sql_columns(migration[table_name])) == [
            column.name for column in model.__table__.columns
        ]


def test_migration_and_models_have_same_nullability_for_every_table():
    migration = _migration_tables()
    for table_name, model in _MODELS.items():
        expected = {
            name: "NOT NULL" not in definition.upper()
            and "PRIMARY KEY" not in definition.upper()
            for name, definition in _sql_columns(migration[table_name]).items()
        }
        actual = {column.name: column.nullable for column in model.__table__.columns}
        assert actual == expected, table_name


def test_migration_and_models_have_same_checks_and_unique_columns():
    migration = _migration_tables()
    for table_name, model in _MODELS.items():
        assert _model_checks(model) == _sql_checks(migration[table_name]), table_name
        assert _model_unique_columns(model) == _sql_unique_columns(
            migration[table_name]
        ), table_name


def test_sql_types_and_foreign_key_match_migration():
    assert isinstance(Task.__table__.c.value_cad.type, Numeric)
    assert isinstance(Task.__table__.c.track.type, Text)
    assert isinstance(Milestone.__table__.c.gcal_state.type, Text)
    assert isinstance(Span.__table__.c.kind.type, Text)
    assert isinstance(Role.__table__.c.stage.type, Text)
    assert isinstance(Viewer.__table__.c.name.type, Text)

    foreign_key = next(iter(Role.__table__.c.span_id.foreign_keys))
    assert foreign_key.target_fullname == "moving.spans.id"
    assert foreign_key.ondelete == "SET NULL"


def test_server_defaults_match_except_sqlite_safe_client_factories():
    migration = _migration_tables()
    differences: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for table_name, model in _MODELS.items():
        definitions = _sql_columns(migration[table_name])
        for column in model.__table__.columns:
            sql_default = _sql_default(definitions[column.name])
            model_default = (
                str(column.server_default.arg) if column.server_default else None
            )
            if sql_default != model_default:
                differences[(table_name, column.name)] = (sql_default, model_default)

    assert differences == {
        ("tasks", "id"): ("gen_random_uuid()", None),
        ("tasks", "created_at"): ("now()", None),
        ("milestones", "id"): ("gen_random_uuid()", None),
        ("spans", "id"): ("gen_random_uuid()", None),
        ("roles", "id"): ("gen_random_uuid()", None),
        ("collision_acks", "acked_at"): ("now()", None),
        ("gcal_tombstones", "created_at"): ("now()", None),
    }

    task = Task(title="Pack records")
    assert task.id
    assert task.created_at.tzinfo is timezone.utc
    milestone = Milestone(title="Leave", occurs_on=date(2026, 8, 16))
    assert milestone.id
    assert milestone.owner == "both"
    assert milestone.gcal_state == "queued"
    tombstone = GcalTombstone(event_id="event-1")
    assert tombstone.created_at.tzinfo is timezone.utc


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO tasks (id, track, title, created_at) "
        "VALUES ('1', 'money', 'Bad track', CURRENT_TIMESTAMP)",
        "INSERT INTO tasks (id, title, owner, created_at) "
        "VALUES ('2', 'Bad owner', 'nobody', CURRENT_TIMESTAMP)",
        "INSERT INTO tasks (id, title, owner, created_at) "
        "VALUES ('3', 'Null owner', NULL, CURRENT_TIMESTAMP)",
        "INSERT INTO milestones (id, title, occurs_on, gcal_state) "
        "VALUES ('4', 'Bad state', '2026-08-16', 'lost')",
        "INSERT INTO spans (id, kind, label, starts_on, ends_on) "
        "VALUES ('5', 'pause', 'Bad kind', '2026-08-16', '2026-08-17')",
        "INSERT INTO spans (id, kind, label, starts_on, ends_on) "
        "VALUES ('6', 'move', 'Backwards', '2026-08-17', '2026-08-16')",
        "INSERT INTO spans (id, kind, label, starts_on, ends_on) "
        "VALUES ('7', NULL, 'Null kind', '2026-08-16', '2026-08-17')",
        "INSERT INTO roles (id, company, title, owner) "
        "VALUES ('8', 'Acme', 'Builder', 'nobody')",
        "INSERT INTO roles (id, company, title, stage) "
        "VALUES ('9', 'Acme', 'Builder', 'maybe')",
        "INSERT INTO viewers (email, name) VALUES ('a@example.test', 'nobody')",
        "INSERT INTO collision_acks (item1_id, item2_id, acked_by, acked_at) "
        "VALUES ('a', 'b', 'nobody', CURRENT_TIMESTAMP)",
        "INSERT INTO collision_acks (item1_id, item2_id, acked_by, acked_at) "
        "VALUES ('b', 'a', 'joe', CURRENT_TIMESTAMP)",
    ],
)
def test_sqlite_rejects_invalid_constrained_values(session: Session, statement: str):
    with pytest.raises(IntegrityError):
        session.exec(text(statement))
        session.commit()
    session.rollback()


def test_sqlite_rejects_duplicate_calendar_event_id(session: Session):
    session.add_all(
        [
            Milestone(
                title="First", occurs_on=date(2026, 8, 16), gcal_event_id="event-1"
            ),
            Milestone(
                title="Second", occurs_on=date(2026, 8, 17), gcal_event_id="event-1"
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
