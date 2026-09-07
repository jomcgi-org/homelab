"""Merged pull request snapshot model, title parser, and database writer."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel, Session, delete

_TITLE_RE = re.compile(r"^(feat|fix|docs|chore|test|refactor)(?:\(([^)]+)\))?!?:\s+.+$")
_AGENT_MARKERS = (
    "generated with [claude code]",
    "claude.ai/code",
    "codex",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MergedPR(SQLModel, table=True):
    __tablename__ = "merged_prs"
    __table_args__ = {"schema": "observability", "extend_existing": True}

    number: int = Field(primary_key=True)
    title: str
    merged_at: datetime = Field(sa_type=DateTime(timezone=True))
    additions: int
    deletions: int
    changed_files: int
    type: str
    scope: str | None = None
    agent_authored: bool
    snapshotted_at: datetime = Field(
        default_factory=_utc_now,
        sa_type=DateTime(timezone=True),
    )


def parse_title(title: str) -> tuple[str, str | None]:
    """Return the supported Conventional Commit type and optional scope."""
    match = _TITLE_RE.fullmatch(title)
    if match is None:
        return ("other", None)
    return (match.group(1), match.group(2))


def is_agent_authored(body: str) -> bool:
    """Return whether a pull request body contains a known agent marker."""
    folded = body.casefold()
    return any(marker in folded for marker in _AGENT_MARKERS)


def upsert_and_prune(
    session: Session,
    pulls: list[dict],
    cutoff: datetime,
    *,
    snapshotted_at: datetime | None = None,
) -> tuple[int, int]:
    """Upsert fetched pulls and delete snapshots older than ``cutoff``."""
    snapshot_time = snapshotted_at or _utc_now()
    for pull in pulls:
        merged_at = pull["merged_at"]
        if isinstance(merged_at, str):
            merged_at = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        type_, scope = parse_title(str(pull["title"]))
        session.merge(
            MergedPR(
                number=int(pull["number"]),
                title=str(pull["title"]),
                merged_at=merged_at,
                additions=int(pull["additions"]),
                deletions=int(pull["deletions"]),
                changed_files=int(pull["changed_files"]),
                type=type_,
                scope=scope,
                agent_authored=is_agent_authored(str(pull.get("body") or "")),
                snapshotted_at=snapshot_time,
            )
        )

    result = session.exec(delete(MergedPR).where(MergedPR.merged_at < cutoff))
    deleted = result.rowcount or 0
    session.commit()
    return (len(pulls), deleted)


def write_snapshot(pulls: list[dict], cutoff: datetime) -> tuple[int, int]:
    """Open a fresh session, then persist and prune one snapshot batch."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        return upsert_and_prune(session, pulls, cutoff)
