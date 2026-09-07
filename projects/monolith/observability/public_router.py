"""Public read-only API for merged pull request snapshots."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from email.utils import format_datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlmodel import Session, select

from core.db import get_session
from knowledge.http_cache import _GRAPH_CACHE_CONTROL
from observability.merged_prs import MergedPR

router = APIRouter(prefix="/api/agents/public", tags=["observability"])

_TYPES = ("feat", "fix", "docs", "chore", "test", "refactor", "other")
_CACHE_CONTROL = _GRAPH_CACHE_CONTROL


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    coerced = _as_utc(value)
    if coerced is None:
        return None
    return coerced.isoformat().replace("+00:00", "Z")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _payload(session: Session, now: datetime) -> tuple[dict, datetime | None]:
    now = _as_utc(now) or _now_utc()
    first_day = now.date() - timedelta(days=29)
    cutoff_30d = datetime.combine(first_day, time.min, tzinfo=timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    rows = list(
        session.exec(
            select(MergedPR)
            .where(MergedPR.merged_at >= cutoff_30d)
            .order_by(MergedPR.merged_at.desc())
        ).all()
    )

    daily_by_date = {
        first_day + timedelta(days=offset): {type_: 0 for type_ in _TYPES}
        for offset in range(30)
    }
    for row in rows:
        merged_at = _as_utc(row.merged_at)
        if merged_at is not None and merged_at.date() in daily_by_date:
            daily_by_date[merged_at.date()][row.type] += 1

    daily = [
        {"d": day.isoformat(), **daily_by_date[day]} for day in sorted(daily_by_date)
    ]
    week_rows = [
        row
        for row in rows
        if (_as_utc(row.merged_at) or datetime.min.replace(tzinfo=timezone.utc))
        >= cutoff_7d
    ]
    week = [
        {
            "number": row.number,
            "title": row.title,
            "merged_at": _iso(row.merged_at),
            "additions": row.additions,
            "deletions": row.deletions,
            "changed_files": row.changed_files,
            "type": row.type,
            "scope": row.scope,
            "agent_authored": row.agent_authored,
        }
        for row in week_rows
    ]
    totals = {
        "n_7d": len(week_rows),
        "agent_7d": sum(row.agent_authored for row in week_rows),
        "add_7d": sum(row.additions for row in week_rows),
        "del_7d": sum(row.deletions for row in week_rows),
        "n_30d": len(rows),
    }
    snapshotted_at = _as_utc(max((row.snapshotted_at for row in rows), default=None))
    return ({"daily": daily, "week": week, "totals": totals}, snapshotted_at)


@router.get("/merges")
def get_public_merges(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Return cached 30-day aggregates and the last seven days of merges."""
    now = _now_utc()
    payload, snapshotted_at = _payload(session, now)
    stamp = _iso(snapshotted_at) or "null"
    etag = f'"merges-v1-{now.date().isoformat()}-{stamp}-{payload["totals"]["n_30d"]}"'
    headers = {"Cache-Control": _CACHE_CONTROL, "ETag": etag}
    if snapshotted_at is not None:
        headers["Last-Modified"] = format_datetime(snapshotted_at, usegmt=True)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    return payload
