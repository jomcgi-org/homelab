"""Database writer for merged pull request snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from observability.merged_prs import MergedPR, is_agent_authored, parse_title

_UPSERT_CHUNK_SIZE = 200
_MUTABLE_FIELDS = (
    "title",
    "additions",
    "deletions",
    "changed_files",
    "type",
    "scope",
    "agent_authored",
    "snapshotted_at",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_and_prune(
    session: Session,
    pulls: list[dict],
    cutoff: datetime,
    *,
    snapshotted_at: datetime | None = None,
) -> tuple[int, int]:
    """Batch-upsert fetched pulls and delete snapshots older than ``cutoff``."""
    snapshot_time = snapshotted_at or _utc_now()
    rows = []
    for pull in pulls:
        merged_at = pull["merged_at"]
        if isinstance(merged_at, str):
            merged_at = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        type_, scope = parse_title(str(pull["title"]))
        rows.append(
            {
                "number": int(pull["number"]),
                "title": str(pull["title"]),
                "merged_at": merged_at,
                "additions": int(pull["additions"]),
                "deletions": int(pull["deletions"]),
                "changed_files": int(pull["changed_files"]),
                "type": type_,
                "scope": scope,
                "agent_authored": is_agent_authored(str(pull.get("body") or "")),
                "snapshotted_at": snapshot_time,
            }
        )

    table = MergedPR.__table__
    # SQLite has a dialect-specific insert helper with the same conflict API,
    # which keeps the file-backed hermetic tests on the production batch path.
    insert_fn = (
        sqlite_insert
        if session.get_bind().dialect.name == "sqlite"
        else postgresql_insert
    )
    for offset in range(0, len(rows), _UPSERT_CHUNK_SIZE):
        statement = insert_fn(table).values(rows[offset : offset + _UPSERT_CHUNK_SIZE])
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.number],
            set_={
                field: getattr(statement.excluded, field) for field in _MUTABLE_FIELDS
            },
        )
        session.exec(statement)

    result = session.exec(delete(MergedPR).where(MergedPR.merged_at < cutoff))
    deleted = result.rowcount or 0
    session.commit()
    return (len(pulls), deleted)


def write_snapshot(pulls: list[dict], cutoff: datetime) -> tuple[int, int]:
    """Open a fresh session, then persist and prune one snapshot batch."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        return upsert_and_prune(session, pulls, cutoff)
