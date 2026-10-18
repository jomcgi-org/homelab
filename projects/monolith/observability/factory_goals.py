"""Orchestrator-declared factory goals and their deterministic progress.

The factory goal panel used to infer intent from merged PRs. Goals are
intent, so the orchestrator declares them as rows in
``observability.factory_goals`` (see
``chart/migrations/20260924070000_factory_goals.sql``) and the panel renders
those rows. Progress uses linked issue state in ``factory_goal_issues`` and
distinct merged PR numbers: title references in ``merged_prs`` or GitHub's
merged closing refs. Last activity includes issue closure and merge times.

Every shaping helper takes plain dicts so the unit tests run without a
database. Only ``list_active_goals`` and ``list_goal_issues`` touch a
session, and they read granted tables and nothing else, which keeps the
public tier on plain SQL over granted tables.
"""

from __future__ import annotations

import re
from core.clock import as_utc, iso
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, JSON
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlmodel import Field, Session, SQLModel, select

_INT_ARRAY = PG_ARRAY(Integer).with_variant(JSON(), "sqlite")

MAX_ACTIVE_GOALS = 5
STALE_AFTER_DAYS = 14
MAX_STATEMENT_CHARS = 280

_ISSUE_REF_RE = re.compile(r"#(\d+)")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FactoryGoal(SQLModel, table=True):
    __tablename__ = "factory_goals"
    __table_args__ = {"schema": "observability", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    statement: str
    issue_numbers: list[int] = Field(default_factory=list, sa_column=Column(_INT_ARRAY))
    declared_by: str
    declared_at: datetime = Field(
        default_factory=_utc_now,
        sa_type=DateTime(timezone=True),
    )
    active: bool = True


class FactoryGoalIssue(SQLModel, table=True):
    __tablename__ = "factory_goal_issues"
    __table_args__ = {"schema": "observability", "extend_existing": True}

    number: int = Field(primary_key=True)
    state: str
    closed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    closing_prs: list[int] = Field(
        default_factory=list, sa_column=Column(_INT_ARRAY, nullable=False)
    )
    last_closing_merge_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    snapshotted_at: datetime = Field(sa_type=DateTime(timezone=True))


def _iso(value: datetime | None) -> str | None:
    stamp = iso(value)
    return None if stamp is None else stamp.replace("+00:00", "Z")


def validate_goals(goals: list[dict], declared_by: str) -> list[dict]:
    """Check a replacement goal set, returning cleaned rows or raising ValueError."""
    if not isinstance(declared_by, str) or not declared_by.strip():
        raise ValueError("declared_by must be a non-empty string")
    if not isinstance(goals, list) or not goals:
        raise ValueError("goals must be a non-empty list")
    if len(goals) > MAX_ACTIVE_GOALS:
        raise ValueError(f"at most {MAX_ACTIVE_GOALS} active goals are supported")
    cleaned = []
    for index, goal in enumerate(goals):
        if not isinstance(goal, dict):
            raise ValueError(f"goal {index} must be an object")
        statement = goal.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError(f"goal {index} needs a non-empty statement")
        statement = statement.strip()
        if len(statement) > MAX_STATEMENT_CHARS:
            raise ValueError(
                f"goal {index} statement exceeds {MAX_STATEMENT_CHARS} chars"
            )
        issues = goal.get("issue_numbers", [])
        if not isinstance(issues, list) or not issues:
            raise ValueError(f"goal {index} needs at least one linked issue")
        numbers = []
        for issue in issues:
            if type(issue) is not int:
                raise ValueError(f"goal {index} issue numbers must be integers")
            numbers.append(issue)
        cleaned.append(
            {
                "statement": statement,
                "issue_numbers": numbers,
                "declared_by": declared_by.strip(),
            }
        )
    return cleaned


def _referenced_issues(title: str | None) -> set[int]:
    if not isinstance(title, str):
        return set()
    return {int(match) for match in _ISSUE_REF_RE.findall(title)}


def score_goals(
    goals: list[dict],
    merges: list[dict],
    now: datetime,
    issues: list[dict] | None = None,
) -> list[dict]:
    """Score declared goals against merged PRs, newest declaration first."""
    now_utc = as_utc(now) or _utc_now()
    issues_by_number = {row["number"]: row for row in issues or []}
    scored = []
    for goal in goals:
        linked = set(goal.get("issue_numbers") or [])
        hits = [
            row
            for row in merges
            if linked and linked & _referenced_issues(row.get("title"))
        ]
        snapshots = [issues_by_number[n] for n in linked if n in issues_by_number]
        refs = {row["number"] for row in hits}
        hit_times = []
        for issue in snapshots:
            refs.update(issue["closing_prs"])
            for field in ("closed_at", "last_closing_merge_at"):
                stamp = as_utc(issue.get(field))
                if stamp is not None:
                    hit_times.append(stamp)
        for row in hits:
            merged_at = row.get("merged_at")
            if isinstance(merged_at, str):
                try:
                    merged_at = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
            merged_at = as_utc(merged_at)
            if merged_at is not None:
                hit_times.append(merged_at)
        declared_at = as_utc(goal.get("declared_at"))
        age_days = (
            max(0, (now_utc - declared_at).days) if declared_at is not None else None
        )
        scored.append(
            {
                "id": goal.get("id"),
                "statement": goal.get("statement"),
                "issue_numbers": sorted(linked),
                "declared_by": goal.get("declared_by"),
                "declared_at": _iso(declared_at),
                "age_days": age_days,
                "stale": age_days is None or age_days > STALE_AFTER_DAYS,
                "linked_issues": len(linked),
                "issues_open": sum(row["state"] == "OPEN" for row in snapshots),
                "issues_closed": sum(row["state"] == "CLOSED" for row in snapshots),
                "issues_unknown": len(linked) - len(snapshots),
                "merged_refs": len(refs),
                "last_activity": _iso(max(hit_times)) if hit_times else None,
            }
        )
    scored.sort(key=lambda row: row["declared_at"] or "", reverse=True)
    return scored


def goals_payload(
    goals: list[dict],
    merges: list[dict],
    now: datetime,
    issues: list[dict] | None = None,
) -> dict:
    """Shape the public goals payload with declaration-age freshness."""
    scored = score_goals(goals, merges, now, issues)
    stamps = [row["declared_at"] for row in scored if row["declared_at"]]
    newest = max(stamps) if stamps else None
    return {
        "goals": scored,
        "declared_at": newest,
        "stale": not scored or all(row["stale"] for row in scored),
    }


def list_active_goals(session: Session) -> list[dict]:
    """Return active declared goals, newest declaration first."""
    rows = list(
        session.exec(
            select(FactoryGoal)
            .where(FactoryGoal.active == True)  # noqa: E712
            .order_by(FactoryGoal.declared_at.desc())
        ).all()
    )
    return [
        {
            "id": row.id,
            "statement": row.statement,
            "issue_numbers": list(row.issue_numbers or []),
            "declared_by": row.declared_by,
            "declared_at": as_utc(row.declared_at),
        }
        for row in rows
    ]


def list_goal_issues(session: Session, numbers: set[int]) -> list[dict]:
    """Read the public issue snapshots linked by the active declarations."""
    if not numbers:
        return []
    rows = session.exec(
        select(FactoryGoalIssue).where(FactoryGoalIssue.number.in_(numbers))
    ).all()
    return [row.model_dump() for row in rows]
