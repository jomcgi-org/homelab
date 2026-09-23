from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from observability.factory_goals import (
    MAX_ACTIVE_GOALS,
    STALE_AFTER_DAYS,
    FactoryGoal,
    goals_payload,
    list_active_goals,
    score_goals,
    validate_goals,
)

_NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'factory-goals.db'}")
    table = FactoryGoal.__table__
    original_schema = table.schema
    table.schema = None
    try:
        SQLModel.metadata.create_all(engine, tables=[table])
        with Session(engine) as session:
            yield session
    finally:
        table.schema = original_schema
        engine.dispose()


def _goal(**overrides):
    values = {
        "statement": "Land orchestrator-declared goals",
        "issue_numbers": [5927],
        "declared_by": "opus",
        "declared_at": _NOW - timedelta(days=1),
        "active": True,
    }
    values.update(overrides)
    return FactoryGoal(**values)


def _merge(title, age):
    return {"title": title, "merged_at": (_NOW - age).isoformat()}


def test_validate_goals_rejects_bad_sets():
    with pytest.raises(ValueError):
        validate_goals([], "opus")
    with pytest.raises(ValueError):
        too_many = [{"statement": "x", "issue_numbers": [1]}] * (MAX_ACTIVE_GOALS + 1)
        validate_goals(too_many, "opus")
    with pytest.raises(ValueError):
        validate_goals([{"statement": "  ", "issue_numbers": [1]}], "opus")
    with pytest.raises(ValueError):
        validate_goals([{"statement": "x", "issue_numbers": []}], "opus")
    with pytest.raises(ValueError):
        validate_goals([{"statement": "x", "issue_numbers": ["1"]}], "opus")
    with pytest.raises(ValueError):
        validate_goals([{"statement": "x", "issue_numbers": [1]}], "  ")


def test_validate_goals_cleans_a_replacement_set():
    cleaned = validate_goals(
        [{"statement": "  Ship goals  ", "issue_numbers": [5927, 5784]}],
        "  opus ",
    )
    assert cleaned == [
        {
            "statement": "Ship goals",
            "issue_numbers": [5927, 5784],
            "declared_by": "opus",
        }
    ]


def test_score_goals_counts_issue_refs_and_marks_stale():
    goals = [
        {
            "id": 1,
            "statement": "Fresh goal",
            "issue_numbers": [5927],
            "declared_by": "opus",
            "declared_at": _NOW - timedelta(days=1),
        },
        {
            "id": 2,
            "statement": "Stale goal",
            "issue_numbers": [5784],
            "declared_by": "opus",
            "declared_at": _NOW - timedelta(days=STALE_AFTER_DAYS + 1),
        },
    ]
    merges = [
        _merge("feat(factory): declared goals for #5927", timedelta(hours=2)),
        _merge("fix(factory): unrelated tuneup", timedelta(hours=3)),
    ]
    scored = score_goals(goals, merges, _NOW)
    assert [row["id"] for row in scored] == [1, 2]
    fresh, stale = scored
    assert fresh["merged_refs"] == 1
    assert fresh["last_activity"] == merges[0]["merged_at"].replace("+00:00", "Z")
    assert fresh["stale"] is False
    assert fresh["age_days"] == 1
    assert stale["merged_refs"] == 0
    assert stale["last_activity"] is None
    assert stale["stale"] is True


def test_goals_payload_reports_declaration_freshness():
    payload = goals_payload(
        [
            {
                "id": 1,
                "statement": "Only goal",
                "issue_numbers": [5927],
                "declared_by": "opus",
                "declared_at": _NOW - timedelta(days=1),
            }
        ],
        [],
        _NOW,
    )
    assert payload["declared_at"] == "2026-09-22T12:00:00Z"
    assert payload["stale"] is False


def test_goals_payload_is_stale_with_no_goals():
    assert goals_payload([], [], _NOW)["stale"] is True


def test_list_active_goals_reads_only_active_newest_first(session):
    session.add(_goal(statement="Old", declared_at=_NOW - timedelta(days=2)))
    session.add(_goal(statement="New", declared_at=_NOW - timedelta(days=1)))
    session.add(_goal(statement="Retired", active=False))
    session.commit()

    rows = list_active_goals(session)
    assert [row["statement"] for row in rows] == ["New", "Old"]
    assert rows[0]["issue_numbers"] == [5927]
