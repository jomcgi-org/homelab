"""Scheduled Google Calendar synchronization for moving milestones."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx
from sqlalchemy import delete
from sqlmodel import Session, select

from moving.models import GcalTombstone, Milestone
from shared import google_calendar

logger = logging.getLogger("monolith.moving.gcal_sync")

GCAL_TIMEOUT_SECS = 30.0


@dataclass(frozen=True)
class _MilestoneSnapshot:
    id: str
    title: str
    occurs_on: date
    gcal_event_id: str | None
    gcal_state: str


@dataclass(frozen=True)
class _MilestoneUpdate:
    id: str
    event_id: str
    synced_at: datetime


@dataclass
class _SyncOutcome:
    milestone_updates: list[_MilestoneUpdate] = field(default_factory=list)
    deleted_tombstones: list[str] = field(default_factory=list)


def _calendar_id() -> str:
    return (
        os.environ.get("MOVING_GCAL_CALENDAR_ID")
        or os.environ.get("GOOGLE_CALENDAR_ID")
        or "primary"
    )


def _snapshot_milestones(session: Session) -> list[_MilestoneSnapshot]:
    rows = session.exec(
        select(Milestone).where(Milestone.gcal_state.in_(("queued", "synced")))
    ).all()
    return [
        _MilestoneSnapshot(
            id=row.id,
            title=row.title,
            occurs_on=row.occurs_on,
            gcal_event_id=row.gcal_event_id,
            gcal_state=row.gcal_state,
        )
        for row in rows
    ]


def _snapshot_tombstones(session: Session) -> list[str]:
    return list(session.exec(select(GcalTombstone.event_id)).all())


def _load_milestones() -> list[_MilestoneSnapshot]:
    """Load milestone snapshots in a fresh worker-thread session."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        return _snapshot_milestones(session)


def _load_tombstones() -> list[str]:
    """Load tombstone snapshots in a fresh worker-thread session."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        return _snapshot_tombstones(session)


def _is_gone(error: Exception) -> bool:
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (
        404,
        410,
    )


async def _insert_milestone(
    client: httpx.AsyncClient, cal_id: str, milestone: _MilestoneSnapshot
) -> _MilestoneUpdate:
    event_id = await google_calendar.insert_event(
        client,
        cal_id,
        milestone.title,
        milestone.occurs_on,
    )
    return _MilestoneUpdate(
        id=milestone.id,
        event_id=event_id,
        synced_at=datetime.now(timezone.utc),
    )


async def _run_network_sync(
    client: httpx.AsyncClient,
    cal_id: str,
    milestones: list[_MilestoneSnapshot],
    tombstones: list[str],
) -> _SyncOutcome:
    outcome = _SyncOutcome()
    for milestone in milestones:
        try:
            if milestone.gcal_state == "queued" or milestone.gcal_event_id is None:
                outcome.milestone_updates.append(
                    await _insert_milestone(client, cal_id, milestone)
                )
                continue

            try:
                event = await google_calendar.get_event(
                    client, cal_id, milestone.gcal_event_id
                )
            except Exception as error:
                if not _is_gone(error):
                    raise
                outcome.milestone_updates.append(
                    await _insert_milestone(client, cal_id, milestone)
                )
                continue

            if (
                event.get("summary") != milestone.title
                or event.get("start", {}).get("date") != milestone.occurs_on.isoformat()
            ):
                await google_calendar.patch_event(
                    client,
                    cal_id,
                    milestone.gcal_event_id,
                    milestone.title,
                    milestone.occurs_on,
                )
                outcome.milestone_updates.append(
                    _MilestoneUpdate(
                        id=milestone.id,
                        event_id=milestone.gcal_event_id,
                        synced_at=datetime.now(timezone.utc),
                    )
                )
        except Exception:
            logger.exception("moving gcal sync: milestone %s failed", milestone.id)

    for event_id in tombstones:
        try:
            await google_calendar.delete_event(client, cal_id, event_id)
            outcome.deleted_tombstones.append(event_id)
        except Exception:
            logger.exception("moving gcal sync: tombstone %s failed", event_id)

    return outcome


def _apply_sync(session: Session, outcome: _SyncOutcome) -> int:
    update_ids = [update.id for update in outcome.milestone_updates]
    milestones = (
        session.exec(select(Milestone).where(Milestone.id.in_(update_ids))).all()
        if update_ids
        else []
    )
    updates_by_id = {update.id: update for update in outcome.milestone_updates}
    touched = 0
    for milestone in milestones:
        update = updates_by_id[milestone.id]
        milestone.gcal_event_id = update.event_id
        milestone.gcal_synced_at = update.synced_at
        milestone.gcal_state = "synced"
        touched += 1

    if outcome.deleted_tombstones:
        result = session.execute(
            delete(GcalTombstone).where(
                GcalTombstone.event_id.in_(outcome.deleted_tombstones)
            )
        )
        touched += result.rowcount or 0
    return touched


def _sync_helper(outcome: _SyncOutcome) -> int:
    """Persist sync results in a fresh worker-thread session."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        touched = _apply_sync(session, outcome)
        session.commit()
    return touched


def sync_milestones(session: Session) -> int:
    """Synchronize milestones and tombstones using an explicit test session."""
    if not google_calendar.configured():
        return 0
    milestones = _snapshot_milestones(session)
    tombstones = _snapshot_tombstones(session)

    async def run() -> _SyncOutcome:
        async with httpx.AsyncClient(timeout=GCAL_TIMEOUT_SECS) as client:
            return await _run_network_sync(
                client, _calendar_id(), milestones, tombstones
            )

    outcome = asyncio.run(run())
    touched = _apply_sync(session, outcome)
    session.commit()
    return touched


async def gcal_sync_handler(session: Session) -> datetime | None:
    """Run the hourly moving milestone sync without using the scheduler session."""
    if not google_calendar.configured():
        logger.info("moving gcal sync: unconfigured, skipping")
        return None

    milestones, tombstones = await asyncio.gather(
        asyncio.to_thread(_load_milestones),
        asyncio.to_thread(_load_tombstones),
    )
    async with httpx.AsyncClient(timeout=GCAL_TIMEOUT_SECS) as client:
        outcome = await _run_network_sync(
            client, _calendar_id(), milestones, tombstones
        )
    touched = await asyncio.to_thread(_sync_helper, outcome)
    logger.info(
        "moving gcal sync: checked %d milestones and %d tombstones, touched %d rows",
        len(milestones),
        len(tombstones),
        touched,
    )
    return None
