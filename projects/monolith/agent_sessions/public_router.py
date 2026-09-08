"""Public, aggregate-only agent activity API."""

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

    return {
        "now": {
            "active_last_hour": int(_value(now_row, "active_last_hour") or 0),
            "sessions_today": int(_value(now_row, "sessions_today") or 0),
            "running": int(_value(now_row, "running") or 0),
            "last_turn_at": _as_utc_iso(_value(now_row, "last_turn_at")),
        },
        "daily": daily,
        "local_daily": local_daily,
        "totals_7d": {
            "ember": _totals(ember_totals_rows, total_fields),
            "local": _totals(local_totals_rows, total_fields),
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
