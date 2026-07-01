"""World Cup 2026 Scotland tracker HTTP API. SSR-only: never added to httproute-public.yaml.

One read endpoint backs the /app/wc2026 page:

- ``GET /api/wc2026/summary`` returns the focus team (Scotland), its group
  table, its qualification probabilities, and the ranked swing matches in a
  single payload, so the SvelteKit SSR loader fetches once and the CDN fans the
  result out.

Reached only from SvelteKit SSR (``http://localhost:8000`` in the same pod); the
/app/wc2026 page is the public surface. Because this JSON endpoint is never
listed on httproute-public.yaml it is unreachable through the public gateway,
which keeps the page unlisted. The ETag folds in the freshest standings stamp
so conditional GETs short-circuit with a 304.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from app.db import get_session
from worldcup.models import Qualification, Standing, SwingMatch

logger = logging.getLogger("monolith.worldcup.router")

router = APIRouter(prefix="/api/wc2026", tags=["worldcup"])

# The poll+simulate refresh runs every 30 min; 5 min edge freshness is plenty
# and keeps the page snappy between refreshes.
_SUMMARY_CACHE_CONTROL = "public, max-age=300"

# The focus team is Scotland; the whole page is built around its group.
_FOCUS_CODE = "SCO"


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-aware UTC.

    Postgres returns tz-aware values; SQLite (tests) can return naive ones even
    though we always write tz-aware UTC. Treat naive as UTC so ETag stamps and
    serialized timestamps stay stable across both backends.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    coerced = _as_utc(value)
    return coerced.isoformat() if coerced is not None else None


def _serialize_standing(row: Standing) -> dict:
    return {
        "team_id": row.team_id,
        "name": row.name,
        "fifa_code": row.fifa_code,
        "flag_url": row.flag_url,
        "group_name": row.group_name,
        "mp": row.mp,
        "w": row.w,
        "d": row.d,
        "l": row.l,
        "pts": row.pts,
        "gf": row.gf,
        "ga": row.ga,
        "gd": row.gd,
    }


def _serialize_qualification(row: Qualification | None) -> dict:
    """Probabilities stay as raw 0..1 floats; the frontend formats to %.

    When the sim has not run yet there is no row, so return a sane default in
    "contention" rather than erroring: Scotland's standings row is the only hard
    guard for whether the page can render.
    """
    if row is None:
        return {
            "prob_qualify": 0.0,
            "prob_top2": 0.0,
            "prob_third": 0.0,
            "status": "contention",
            "n_sims": 0,
            "computed_at": None,
        }
    return {
        "prob_qualify": row.prob_qualify,
        "prob_top2": row.prob_top2,
        "prob_third": row.prob_third,
        "status": row.status,
        "n_sims": row.n_sims,
        "computed_at": _iso(row.computed_at),
    }


def _serialize_swing(row: SwingMatch) -> dict:
    return {
        "match_id": row.match_id,
        "group_name": row.group_name,
        "home_code": row.home_code,
        "away_code": row.away_code,
        "kickoff": _iso(row.kickoff),
        "swing": row.swing,
        "p_qualify_home_win": row.p_qualify_home_win,
        "p_qualify_draw": row.p_qualify_draw,
        "p_qualify_away_win": row.p_qualify_away_win,
        "is_own_match": row.is_own_match,
    }


def _summary_etag(payload: dict) -> str:
    """Data-derived ETag: a hash of the serialized body, so any change to the
    standings/probabilities/swings (or the shape itself) busts every client."""
    body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return f'"{hashlib.sha256(body).hexdigest()}"'


@router.get("/summary")
def get_summary(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Scotland's group table, qualification odds, and ranked swing matches.

    SSR-only, CDN-cached. Returns 503 when Scotland's standings row is absent
    (data not yet populated) because the page cannot render without it.
    """
    focus = session.exec(
        select(Standing).where(Standing.fifa_code == _FOCUS_CODE)
    ).first()
    if focus is None:
        raise HTTPException(status_code=503, detail="worldcup data unavailable")

    group_rows = session.exec(
        select(Standing).where(Standing.group_name == focus.group_name)
    ).all()
    # Points, then goal difference, then goals for, all descending.
    group_rows = sorted(group_rows, key=lambda r: (r.pts, r.gd, r.gf), reverse=True)

    qualification = session.exec(
        select(Qualification).where(Qualification.fifa_code == _FOCUS_CODE)
    ).first()

    swing_rows = session.exec(
        select(SwingMatch).order_by(SwingMatch.swing.desc()).limit(8)
    ).all()

    updated_at: datetime | None = None
    for row in group_rows:
        stamp = _as_utc(row.updated_at)
        if stamp is not None and (updated_at is None or stamp > updated_at):
            updated_at = stamp

    payload = {
        "focus": _FOCUS_CODE,
        "group": focus.group_name,
        "group_table": [_serialize_standing(r) for r in group_rows],
        "qualification": _serialize_qualification(qualification),
        "swing_matches": [_serialize_swing(r) for r in swing_rows],
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }

    etag = _summary_etag(payload)
    headers = {"Cache-Control": _SUMMARY_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return payload
