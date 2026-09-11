"""Public, aggregate-only agent activity API, plus the read-only factory pages.

The factory routes read public_api.factory_*_snapshot and nothing else. Those
rows are built on the private side by agent_sessions/factory_public.py, because
public_reader has no grant on the swarm or agent_sessions schemas and the
factory code is not in the public image at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, text

from core.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents/public", tags=["agents-public"])

_ACTIVITY_CACHE_CONTROL = "public, max-age=300, s-maxage=300"
# The factory board moves on a 2-minute snapshot cadence, so a 60s shared cache
# never serves anything the writer has not had a chance to refresh, while still
# absorbing a burst of readers. FACTORY_ACTIVITY_CACHE_CONTROL in
# frontend/src/lib/cache-headers.js mirrors this; keep the two in sync.
_FACTORY_CACHE_CONTROL = "public, max-age=60, s-maxage=60"
_DAILY_WINDOW_DAYS = 30
_TOTALS_WINDOW_DAYS = 7

_NOW_QUERY = text(
    """
    SELECT active_last_hour, sessions_today, last_turn_at, running
    FROM public_api.agent_activity_now
    """
)
_DAILY_QUERY = text(
    """
    SELECT
        day,
        model,
        sessions,
        turns,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cost_usd,
        list_cost_usd
    FROM public_api.agent_activity_daily
    WHERE day >= CURRENT_DATE - interval '29 days'
    ORDER BY day DESC, model ASC
    """
)
_LOCAL_DAILY_QUERY = text(
    """
    SELECT
        day,
        model,
        source,
        sessions,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        list_cost_usd
    FROM public_api.local_session_activity_daily
    WHERE day >= CURRENT_DATE - interval '29 days'
    ORDER BY day DESC, model ASC, source ASC
    """
)


def _value(row: Any, name: str) -> Any:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping[name]
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def _as_utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _day(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _number(value: Decimal | float | int | None) -> float | None:
    return float(value) if value is not None else None


def _sum_optional(rows: list[dict], field: str) -> float | None:
    values = [row[field] for row in rows if row[field] is not None]
    return float(sum(values)) if values else None


def _totals(rows: list[dict], fields: tuple[str, ...]) -> dict:
    result = {field: sum(row[field] for row in rows) for field in fields}
    result["cost_usd"] = _sum_optional(rows, "cost_usd")
    result["list_cost_usd"] = _sum_optional(rows, "list_cost_usd")
    return result


def _spend(ember_rows: list[dict], local_rows: list[dict]) -> float:
    ember_cost = sum(
        (row["cost_usd"] or 0) + (row["list_cost_usd"] or 0) for row in ember_rows
    )
    local_cost = sum(row["list_cost_usd"] or 0 for row in local_rows)
    return float(ember_cost + local_cost)


def _shape_activity(
    now_row: Any,
    daily_rows: list[Any],
    local_daily_rows: list[Any] | None = None,
    *,
    today: date | None = None,
) -> dict:
    """Shape view rows and enforce the 30-day and 7-day response windows."""
    today = today or datetime.now(timezone.utc).date()
    daily_start = today - timedelta(days=_DAILY_WINDOW_DAYS - 1)
    totals_start = today - timedelta(days=_TOTALS_WINDOW_DAYS - 1)

    daily = []
    for row in daily_rows:
        row_day = _day(_value(row, "day"))
        if row_day < daily_start or row_day > today:
            continue
        daily.append(
            {
                "day": row_day.isoformat(),
                "model": _value(row, "model"),
                "sessions": int(_value(row, "sessions") or 0),
                "turns": int(_value(row, "turns") or 0),
                "input_tokens": int(_value(row, "input_tokens") or 0),
                "output_tokens": int(_value(row, "output_tokens") or 0),
                "cache_read_tokens": int(_value(row, "cache_read_tokens") or 0),
                "cost_usd": _number(_value(row, "cost_usd")),
                "list_cost_usd": _number(_value(row, "list_cost_usd")),
            }
        )
    # Stable two-pass ordering keeps days newest-first and models alphabetical.
    daily.sort(key=lambda row: row["model"])
    daily.sort(key=lambda row: row["day"], reverse=True)

    local_daily = []
    for row in local_daily_rows or []:
        row_day = _day(_value(row, "day"))
        if row_day < daily_start or row_day > today:
            continue
        local_daily.append(
            {
                "day": row_day.isoformat(),
                "model": _value(row, "model"),
                "source": _value(row, "source"),
                "sessions": int(_value(row, "sessions") or 0),
                "turns": 0,
                "input_tokens": int(_value(row, "input_tokens") or 0),
                "output_tokens": int(_value(row, "output_tokens") or 0),
                "cache_read_tokens": int(_value(row, "cache_read_tokens") or 0),
                "cost_usd": None,
                "list_cost_usd": _number(_value(row, "list_cost_usd")),
            }
        )
    local_daily.sort(key=lambda row: (row["model"], row["source"]))
    local_daily.sort(key=lambda row: row["day"], reverse=True)

    ember_totals_rows = [row for row in daily if row["day"] >= totals_start.isoformat()]
    local_totals_rows = [
        row for row in local_daily if row["day"] >= totals_start.isoformat()
    ]
    total_fields = (
        "sessions",
        "turns",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
    )
    ember_totals = _totals(ember_totals_rows, total_fields)
    local_totals = _totals(local_totals_rows, total_fields)
    combined_totals = _totals([*ember_totals_rows, *local_totals_rows], total_fields)
    combined_totals["spend_usd"] = _spend(ember_totals_rows, local_totals_rows)

    spend_by_day = {}
    for row in daily:
        spend_by_day[row["day"]] = spend_by_day.get(row["day"], 0.0) + float(
            (row["cost_usd"] or 0) + (row["list_cost_usd"] or 0)
        )
    for row in local_daily:
        spend_by_day[row["day"]] = spend_by_day.get(row["day"], 0.0) + float(
            row["list_cost_usd"] or 0
        )
    spend_daily = [
        {"day": day, "spend_usd": spend_by_day[day]} for day in sorted(spend_by_day)
    ]

    return {
        "now": {
            "active_last_hour": int(_value(now_row, "active_last_hour") or 0),
            "sessions_today": int(_value(now_row, "sessions_today") or 0),
            "running": int(_value(now_row, "running") or 0),
            "last_turn_at": _as_utc_iso(_value(now_row, "last_turn_at")),
        },
        "daily": daily,
        "local_daily": local_daily,
        "spend_daily": spend_daily,
        "totals_7d": {
            "ember": ember_totals,
            "local": local_totals,
            "combined": combined_totals,
        },
    }


def _activity_etag(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return f'"agent-activity-v1-{digest}"'


@router.get("/activity")
def get_public_agent_activity(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Return identity-free agent activity aggregates from public views."""
    try:
        now_row = session.execute(_NOW_QUERY).one()
        daily_rows = list(session.execute(_DAILY_QUERY).all())
        local_daily_rows = list(session.execute(_LOCAL_DAILY_QUERY).all())
    except SQLAlchemyError as exc:
        logger.warning("public.agent_activity.unavailable", exc_info=exc)
        raise HTTPException(
            status_code=500, detail="agent activity unavailable"
        ) from exc

    payload = _shape_activity(now_row, daily_rows, local_daily_rows)
    etag = _activity_etag(payload)
    headers = {"Cache-Control": _ACTIVITY_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return payload


_FACTORY_ACTIVITY_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.factory_activity_snapshot
    WHERE id = 1
    """
)
_FACTORY_TASK_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.factory_task_snapshot
    WHERE issue_number = :issue_number
    """
)
_FACTORY_SESSION_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.factory_session_snapshot
    WHERE session_key = :session_key
    """
)


def _payload(row: Any) -> dict:
    """JSONB comes back decoded on psycopg; tolerate a driver that returns text."""
    payload = _value(row, "payload")
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return payload


def _factory_etag(kind: str, payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f'"factory-{kind}-v1-{hashlib.sha256(encoded).hexdigest()}"'


def _factory_response(
    request: Request,
    response: Response,
    kind: str,
    payload: dict,
):
    """Serve one snapshot payload with its ETag, 304-ing an unchanged read."""
    etag = _factory_etag(kind, payload)
    headers = {"Cache-Control": _FACTORY_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    return payload


def _factory_row(session: Session, query, params: dict | None = None) -> Any:
    try:
        return session.execute(query, params or {}).first()
    except SQLAlchemyError as exc:
        logger.warning("public.factory_snapshot.unavailable", exc_info=exc)
        raise HTTPException(status_code=500, detail="factory unavailable") from exc


@router.get("/factory/activity")
def get_public_factory_activity(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """The factory board: what is in flight, what is queued, what recently ran."""
    row = _factory_row(session, _FACTORY_ACTIVITY_QUERY)
    if row is None:
        raise HTTPException(status_code=404, detail="no snapshot")
    return _factory_response(request, response, "activity", _payload(row))


@router.get("/factory/tasks/{issue_number}")
def get_public_factory_task(
    issue_number: int,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """One task's walkthrough: the brief, the plan, and every attempt's turns."""
    row = _factory_row(session, _FACTORY_TASK_QUERY, {"issue_number": issue_number})
    if row is None:
        raise HTTPException(status_code=404, detail="unknown task")
    return _factory_response(request, response, "task", _payload(row))


# ``:path`` because a session key is factory:<task_id>:<node_key>:<attempt> and
# a node key may itself contain colons. The key is opaque here: the snapshot
# writer supplies it and this route only looks it up.
@router.get("/factory/sessions/{session_key:path}")
def get_public_factory_session(
    session_key: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """One attempt's full record: every turn with its diff, rationale and usage."""
    row = _factory_row(session, _FACTORY_SESSION_QUERY, {"session_key": session_key})
    if row is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return _factory_response(request, response, "session", _payload(row))
