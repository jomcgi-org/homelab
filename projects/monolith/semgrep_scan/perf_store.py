"""SQLModel definition + upsert store for semgrep.scan_perf.

Mirrors chart/migrations/20260711210000_semgrep_scan_perf.sql. Postgres is the
single source of truth. Two writers populate this table: report.py inserts the
authoritative Route B row at scan-complete (real engine total_time), and the
SMS harvest (perf_harvest.py) upserts rows pulled from the Semgrep API. Because
Route B is authoritative on runtime, an existing route-b row must never be
clobbered by a later managed-scans upsert for the same scan_id (the reverse,
a route-b write landing after a harvested row, should still update in place).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, Session, SQLModel, select


class ScanPerf(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "scan_perf"
    __table_args__ = {"schema": "semgrep", "extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    scan_id: int = Field(index=True)
    environment: str = ""  # 'route-b' | 'managed-scans'
    raw_environment: str = ""
    is_full_scan: bool = False
    branch: str = ""
    scan_ref: str = ""
    commit_sha: str = ""
    total_time: float = 0.0
    findings_total: int = 0
    cli_version: str = ""
    scan_started_at: Optional[datetime] = None
    scan_completed_at: Optional[datetime] = None


def _merge_decision(existing_env: str | None, new_env: str) -> str:
    """Pure decision for how an upsert should treat an existing row.

    Returns 'insert' when there is no existing row for the scan_id, 'skip' when
    the existing row is an authoritative route-b row and the incoming row is
    not (never clobber Route B's real runtime with a harvested value), or
    'update' otherwise.
    """
    if existing_env is None:
        return "insert"
    if existing_env == "route-b" and new_env != "route-b":
        return "skip"
    return "update"


_UPDATE_FIELDS = (
    "environment",
    "raw_environment",
    "is_full_scan",
    "branch",
    "scan_ref",
    "commit_sha",
    "total_time",
    "findings_total",
    "cli_version",
    "scan_started_at",
    "scan_completed_at",
)


def upsert_scan_perf(session: Session, row: ScanPerf) -> None:
    """Insert or update a scan_perf row by scan_id, per _merge_decision."""
    existing = session.exec(
        select(ScanPerf).where(ScanPerf.scan_id == row.scan_id)
    ).first()

    decision = _merge_decision(
        existing.environment if existing else None, row.environment
    )

    if decision == "insert":
        session.add(row)
    elif decision == "skip":
        return
    else:  # 'update'
        for field in _UPDATE_FIELDS:
            setattr(existing, field, getattr(row, field))
        session.add(existing)

    session.commit()
