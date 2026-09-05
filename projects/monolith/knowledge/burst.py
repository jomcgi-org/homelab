"""Bounded operator grants for knowledge extraction bursts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlmodel import Session

from knowledge.extraction import KG_NODE_KEY

KG_BURST_MAX_EXTRA_JOBS = 1_000
KG_BURST_MAX_DURATION_SECONDS = 24 * 60 * 60


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class KGBurstState:
    active: bool = False
    extra_jobs: int = 0
    used_jobs: int = 0
    remaining_jobs: int = 0
    created_at: datetime | None = None
    expires_at: datetime | None = None
    created_by: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def validate_kg_burst_grant(extra_jobs: int, duration_seconds: int) -> None:
    if extra_jobs < 1:
        raise ValueError("extra_jobs must be at least 1")
    if extra_jobs > KG_BURST_MAX_EXTRA_JOBS:
        raise ValueError(f"extra_jobs must not exceed {KG_BURST_MAX_EXTRA_JOBS}")
    if duration_seconds < 1:
        raise ValueError("duration_seconds must be at least 1")
    if duration_seconds > KG_BURST_MAX_DURATION_SECONDS:
        raise ValueError(
            "duration_seconds must not exceed "
            f"{KG_BURST_MAX_DURATION_SECONDS} (24 hours)"
        )


def kg_burst_state(session: Session, *, now: datetime | None = None) -> KGBurstState:
    """Return the newest unexpired grant and its remaining allowance."""
    checked_at = _as_utc(now or datetime.now(timezone.utc))
    row = session.execute(
        text(
            """
            SELECT burst_grant.extra_jobs,
                   burst_grant.created_at,
                   burst_grant.expires_at,
                   burst_grant.created_by,
                   (
                       SELECT count(*)
                         FROM agent_sessions.agent_sessions AS agent_session
                        WHERE agent_session.node_key = :node_key
                          AND agent_session.created_at >= burst_grant.created_at
                   ) AS used_jobs
              FROM knowledge.kg_burst_grants AS burst_grant
             WHERE burst_grant.expires_at > :checked_at
             ORDER BY burst_grant.created_at DESC
             LIMIT 1
            """
        ),
        {"node_key": KG_NODE_KEY, "checked_at": checked_at},
    ).one_or_none()
    if row is None:
        return KGBurstState()

    expires_at = _as_utc(row.expires_at)
    used_jobs = max(0, int(row.used_jobs))
    remaining_jobs = max(0, int(row.extra_jobs) - used_jobs)
    # Keep expiry enforcement in Python as well as SQL. This protects callers
    # using a test double or a stale transaction snapshot from treating a
    # forgotten grant as permanent allowance.
    active = expires_at > checked_at and remaining_jobs > 0
    if not active:
        remaining_jobs = 0
    return KGBurstState(
        active=active,
        extra_jobs=int(row.extra_jobs),
        used_jobs=used_jobs,
        remaining_jobs=remaining_jobs,
        created_at=_as_utc(row.created_at),
        expires_at=expires_at,
        created_by=row.created_by,
    )


def kg_effective_cap(
    session: Session, base_cap: int, *, now: datetime | None = None
) -> int:
    """Add the full size of a usable burst to the base cap.

    The drainer compares this cap to ``kg_jobs_today()``, which already counts
    every session created since the grant. Adding only the remaining allowance
    subtracted the used jobs on one side while counting them on the other, so
    a grant stalled at half its size (#5778). The grant's own ceiling still
    holds: ``active`` is false once ``remaining_jobs`` reaches zero.
    """
    state = kg_burst_state(session, now=now)
    return base_cap + (state.extra_jobs if state.active else 0)


def create_kg_burst_grant(
    session: Session,
    *,
    extra_jobs: int,
    duration_seconds: int,
    created_by: str,
    now: datetime | None = None,
) -> dict:
    """Persist one bounded grant, rejecting overlap with a usable grant."""
    validate_kg_burst_grant(extra_jobs, duration_seconds)
    created_at = _as_utc(now or datetime.now(timezone.utc))
    if session.get_bind().dialect.name == "postgresql":
        # Serialize operator grants so two simultaneous requests cannot both
        # observe no active grant and create overlapping allowances.
        session.execute(
            text("LOCK TABLE knowledge.kg_burst_grants IN SHARE ROW EXCLUSIVE MODE")
        )
    current = kg_burst_state(session, now=created_at)
    if current.active:
        raise ValueError(
            f"an active kg burst already exists until {current.expires_at.isoformat()}"
        )

    expires_at = created_at + timedelta(seconds=duration_seconds)
    row = session.execute(
        text(
            """
            INSERT INTO knowledge.kg_burst_grants
                (extra_jobs, expires_at, created_at, created_by)
            VALUES
                (:extra_jobs, :expires_at, :created_at, :created_by)
            RETURNING id
            """
        ),
        {
            "extra_jobs": extra_jobs,
            "expires_at": expires_at,
            "created_at": created_at,
            "created_by": created_by,
        },
    ).one()
    session.commit()
    return {
        "grant_id": int(row.id),
        "extra_jobs": extra_jobs,
        "duration_seconds": duration_seconds,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_by": created_by,
    }
