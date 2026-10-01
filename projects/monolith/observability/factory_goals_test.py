from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from observability.factory_goals import (
    MAX_ACTIVE_GOALS,
    STALE_AFTER_DAYS,
    FactoryGoal,
    FactoryGoalIssue,
    goals_payload,
    list_active_goals,
    score_goals,
    upsert_goal_issues,
    validate_goals,
)

_NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'factory-goals.db'}")
    tables = [FactoryGoal.__table__, FactoryGoalIssue.__table__]
    schemas = [table.schema for table in tables]
    for table in tables:
        table.schema = None
    try:
        SQLModel.metadata.create_all(engine, tables=tables)
        with Session(engine) as session:
            yield session
    finally:
        for table, schema in zip(tables, schemas):
            table.schema = schema
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
    return {"number": 123, "title": title, "merged_at": (_NOW - age).isoformat()}


def _issue(number=5927, **overrides):
    values = {
        "number": number,
        "state": "CLOSED",
        "closed_at": _NOW - timedelta(hours=1),
        "closing_prs": [123],
        "last_closing_merge_at": _NOW - timedelta(hours=2),
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize("title", ["feat(factory): ship goals", "fix: #5927"])
@pytest.mark.parametrize("closed_age", [timedelta(hours=1), timedelta(hours=3)])
def test_closing_refs_count_without_title_and_deduplicate(title, closed_age):
    goal = {"issue_numbers": [5927, 5784, 9999], "declared_at": _NOW}
    merge = _merge(title, timedelta(hours=2))
    issue = _issue(closed_at=_NOW - closed_age)
    opened = _issue(
        5784, state="OPEN", closed_at=None, closing_prs=[], last_closing_merge_at=None
    )
    scored = score_goals([goal], [merge], _NOW, [issue, opened])[0]
    assert scored["linked_issues"] == 3
    assert scored["issues_open"] == 1
    assert scored["issues_closed"] == 1
    assert scored["issues_unknown"] == 1
    assert scored["merged_refs"] == 1
    latest = max(issue["closed_at"], issue["last_closing_merge_at"])
    assert scored["last_activity"] == latest.isoformat().replace("+00:00", "Z")


def test_closing_refs_work_without_retained_merge_rows():
    goal = {"issue_numbers": [5927, 5784], "declared_at": _NOW}
    scored = score_goals([goal], [], _NOW, [_issue(), _issue(5784)])[0]
    assert scored["merged_refs"] == 1
    assert scored["issues_closed"] == 2
    assert scored["last_activity"] == "2026-09-23T11:00:00Z"


def test_unsnapshotted_issues_are_unknown():
    scored = score_goals([{"issue_numbers": [5927]}], [], _NOW)[0]
    assert scored["issues_unknown"] == 1
    assert scored["issues_open"] == scored["issues_closed"] == 0


def test_issue_snapshot_upserts_and_prunes(session):
    assert upsert_goal_issues(session, [_issue(), _issue(5784)], {5927, 5784}) == (2, 0)
    reopened = _issue(
        state="OPEN", closed_at=None, closing_prs=[], last_closing_merge_at=None
    )
    assert upsert_goal_issues(session, [reopened], {5927}) == (1, 1)
    session.expire_all()
    row = session.get(FactoryGoalIssue, 5927)
    assert row.state == "OPEN"
    assert row.closed_at is None
    assert row.closing_prs == []
    assert isinstance(row.snapshotted_at, datetime)
    assert session.get(FactoryGoalIssue, 5784) is None
    # A now-unresolvable linked issue must become unknown, not retain old state.
    assert upsert_goal_issues(session, [], {5927}) == (0, 1)
    assert upsert_goal_issues(session, [_issue()], {5927}) == (1, 0)
    assert upsert_goal_issues(session, [], set()) == (0, 1)


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
