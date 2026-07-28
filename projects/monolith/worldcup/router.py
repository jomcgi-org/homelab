"""World Cup 2026 Scotland tracker HTTP API. SSR-only: never added to httproute-public.yaml.

One read endpoint backs the /app/wc2026 page:

- ``GET /api/wc2026/summary`` returns the whole tournament in a single payload:
  every group's table, every team's qualification probabilities, and each
  contending team's ranked swing matches. The page lets a visitor pick any of
  the 48 teams (Scotland by default) and switches client-side, so the SvelteKit
  SSR loader fetches once and the CDN fans the result out.

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

from core.db import get_session
from worldcup.models import Qualification, Standing, SwingMatch

logger = logging.getLogger("monolith.worldcup.router")

router = APIRouter(prefix="/api/wc2026", tags=["worldcup"])

# The poll+simulate refresh runs every 30 min; 5 min edge freshness is plenty
# and keeps the page snappy between refreshes.
_SUMMARY_CACHE_CONTROL = "public, max-age=300"

# The country the page opens on; the dropdown can switch to any of the 48.
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
    "contention" rather than erroring: the presence of standings is the only hard
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
    """All 48 teams' group tables, qualification odds, and per-country swings.

    SSR-only, CDN-cached. The page lets a visitor pick any country and switches
    client-side, so the whole tournament ships in one payload: group tables for
    every group, qualification for every team, and each team's ranked swing list.
    Returns 503 when no standings exist yet (data not populated) because nothing
    can render. ``default_country`` is the team the page opens on (Scotland).
    """
    standings = session.exec(select(Standing)).all()
    if not standings:
        raise HTTPException(status_code=503, detail="worldcup data unavailable")

    # Group tables: standings grouped by group, each sorted pts/gd/gf desc. The
    # frontend builds the dropdown list and code->group map by flattening this.
    groups: dict[str, list[dict]] = {}
    for row in sorted(standings, key=lambda r: (r.pts, r.gd, r.gf), reverse=True):
        groups.setdefault(row.group_name, []).append(_serialize_standing(row))

    qual_rows = {q.fifa_code: q for q in session.exec(select(Qualification)).all()}
    qualification = {
        r.fifa_code: _serialize_qualification(qual_rows.get(r.fifa_code))
        for r in standings
    }
    # n_sims is shared across every qualification row; surface it once.
    n_sims = next((q.n_sims for q in qual_rows.values()), 0)

    # Each contending team's ranked swing list (clinched/eliminated teams have no
    # rows, so they resolve to an empty list on the page).
    swing_by_country: dict[str, list[dict]] = {}
    for row in session.exec(select(SwingMatch).order_by(SwingMatch.swing.desc())).all():
        swing_by_country.setdefault(row.country_code, []).append(_serialize_swing(row))

    updated_at: datetime | None = None
    for row in standings:
        stamp = _as_utc(row.updated_at)
        if stamp is not None and (updated_at is None or stamp > updated_at):
            updated_at = stamp

    payload = {
        "default_country": _FOCUS_CODE,
        "groups": groups,
        "qualification": qualification,
        "swing_by_country": swing_by_country,
        "n_sims": n_sims,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }

    etag = _summary_etag(payload)
    headers = {"Cache-Control": _SUMMARY_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return payload
