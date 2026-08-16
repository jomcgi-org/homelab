"""Schema-shape and SQLite constraint tests for moving models."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import Numeric, Text, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from moving.models import Milestone, Role, Span, Task, Viewer


def _checks(model: type) -> set[str]:
    checks = {
        str(constraint.sqltext)
        for constraint in model.__table__.constraints
        if hasattr(constraint, "sqltext")
    }
    for column in model.__table__.columns:
        checks.update(str(constraint.sqltext) for constraint in column.constraints)
    return checks


def test_table_names_and_schemas_match_migration():
    expected = {
        Task: "tasks",
        Milestone: "milestones",
        Span: "spans",
        Role: "roles",
        Viewer: "viewers",
    }
    for model, table_name in expected.items():
        assert model.__tablename__ == table_name
        assert model.__table_args__ == {
            "schema": "moving",
            "extend_existing": True,
        }


def test_columns_and_nullability_match_migration():
    assert {column.name: column.nullable for column in Task.__table__.columns} == {
        "id": False,
        "track": True,
        "title": False,
        "note": True,
        "owner": True,
        "due_on": True,
        "done_at": True,
        "value_cad": True,
        "created_at": False,
    }
    assert [column.name for column in Milestone.__table__.columns] == [
        "id",
        "title",
        "occurs_on",
        "owner",
        "gcal_event_id",
        "gcal_synced_at",
        "gcal_state",
    ]
    assert [column.name for column in Span.__table__.columns] == [
        "id",
        "kind",
        "label",
        "starts_on",
        "ends_on",
    ]
    assert [column.name for column in Role.__table__.columns] == [
        "id",
        "company",
        "title",
        "stage",
        "next_on",
        "span_id",
    ]
    assert [column.name for column in Viewer.__table__.columns] == ["email", "name"]
    assert isinstance(Task.__table__.c.value_cad.type, Numeric)
    assert isinstance(Task.__table__.c.track.type, Text)
    assert isinstance(Milestone.__table__.c.gcal_state.type, Text)
    assert isinstance(Span.__table__.c.kind.type, Text)
    assert isinstance(Role.__table__.c.stage.type, Text)
    assert isinstance(Viewer.__table__.c.name.type, Text)


def test_defaults_and_foreign_key_match_migration():
    task = Task(title="Pack records")
    assert task.id
    assert task.created_at.tzinfo is timezone.utc
    milestone = Milestone(title="Leave", occurs_on=datetime.now().date())
    assert milestone.gcal_state == "queued"

    foreign_key = next(iter(Role.__table__.c.span_id.foreign_keys))
    assert foreign_key.target_fullname == "moving.spans.id"
    assert foreign_key.ondelete == "SET NULL"


def test_model_metadata_contains_all_checks():
    assert "track IN ('sell', 'admin', 'ship', 'people')" in _checks(Task)
    assert "owner IN ('joe', 'anna', 'both')" in _checks(Task)
    assert "gcal_state IN ('queued', 'synced', 'held')" in _checks(Milestone)
    assert "kind IN ('visitor', 'work', 'move', 'trip')" in _checks(Span)
    assert "ends_on >= starts_on" in _checks(Span)
    assert "stage IN ('applied', 'screen', 'onsite', 'offer', 'closed')" in _checks(
        Role
    )
    assert "name IN ('joe', 'anna')" in _checks(Viewer)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO tasks (id, track, title, created_at) "
        "VALUES ('1', 'money', 'Bad track', CURRENT_TIMESTAMP)",
        "INSERT INTO tasks (id, title, owner, created_at) "
        "VALUES ('2', 'Bad owner', 'nobody', CURRENT_TIMESTAMP)",
        "INSERT INTO milestones (id, title, occurs_on, gcal_state) "
        "VALUES ('3', 'Bad state', '2026-08-16', 'lost')",
        "INSERT INTO spans (id, kind, label, starts_on, ends_on) "
        "VALUES ('4', 'pause', 'Bad kind', '2026-08-16', '2026-08-17')",
        "INSERT INTO spans (id, kind, label, starts_on, ends_on) "
        "VALUES ('5', 'move', 'Backwards', '2026-08-17', '2026-08-16')",
        "INSERT INTO roles (id, company, title, stage) "
        "VALUES ('6', 'Acme', 'Builder', 'maybe')",
        "INSERT INTO viewers (email, name) VALUES ('a@example.test', 'nobody')",
    ],
)
def test_sqlite_rejects_invalid_constrained_values(session: Session, statement: str):
    with pytest.raises(IntegrityError):
        session.exec(text(statement))
        session.commit()
    session.rollback()
