"""Client for the free public worldcup26.ir tournament API (fetch + parse only).

Three public GET endpoints, no auth:

- ``GET /get/teams`` is the join table: the ONLY place mapping a numeric
  ``team_id`` to a team's ``name_en`` / ``fifa_code`` / ``flag``.
- ``GET /get/groups`` returns per-group standings rows that carry only a
  ``team_id`` plus string-typed numeric stats (no name/code/flag), so each row
  must be joined back to the teams payload by ``team_id``.
- ``GET /get/games`` returns fixtures that carry team NAMES but no FIFA codes,
  so home/away ids are joined to the teams payload to recover the FIFA codes the
  simulation joins on.

The pure parse helpers (build_team_index, parse_standings, parse_fixtures) take
already-decoded payload dicts so the unit tests exercise them with no network.
fetch_all orchestrates the three GETs and never raises: it catches HTTP and
parse errors, records them in the stats dict, and returns whatever it managed to
gather, mirroring dr_jobs/scraper.py (a transient outage must not wipe the
corpus). This module does NO database writes; persistence is a separate concern.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("monolith.worldcup")

# The API serves kickoff times as venue-local wall-clock without an offset. The
# 2026 tournament spans US/Canada/Mexico venues across several zones, but the
# feed exposes only a single "local_date" string. We approximate every venue as
# US Eastern: good enough to order fixtures and render a sensible local time, and
# it normalises to a single, unambiguous UTC instant for storage.
_VENUE_TZ = ZoneInfo("America/New_York")
_LOCAL_DATE_FMT = "%m/%d/%Y %H:%M"

# Per-request timeout; the client-level timeout in the handler is the ceiling.
_TIMEOUT_SECS = 20.0

# Retry transient upstream failures (5xx, timeouts, connection resets) with
# exponential backoff. worldcup26.ir intermittently 500s on a single endpoint,
# and /get/teams is the join table the whole poll depends on, so one blip must
# not collapse the fetch. 4xx responses are NOT retried (a client error will not
# fix itself). Backoff is 0.5s, then 1.0s between the three attempts.
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_SECS = 0.5


def _to_int(value) -> int:
    """Coerce a (possibly string, possibly None) stat to int, defaulting to 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_team_index(teams_payload: dict) -> dict[str, dict]:
    """Map team_id (str) -> {name, fifa_code, flag} from the /get/teams payload.

    This is the join table for both standings and fixtures. Entries missing an
    ``id`` or a ``fifa_code`` are skipped defensively (they would be unjoinable).
    """
    index: dict[str, dict] = {}
    for team in (teams_payload or {}).get("teams", []) or []:
        team_id = team.get("id")
        fifa_code = team.get("fifa_code")
        if not team_id or not fifa_code:
            continue
        index[str(team_id)] = {
            "name": team.get("name_en") or "",
            "fifa_code": fifa_code,
            "flag": team.get("flag") or "",
        }
    return index


def parse_standings(groups_payload: dict, team_index: dict) -> list[dict]:
    """Parse /get/groups into one row per team, joined to team_index for identity.

    Group rows carry only team_id and string-typed stats, so name/fifa_code/flag
    come from team_index[team_id]. Stats are coerced to int defensively. Teams
    absent from the index are skipped (should not happen for a consistent feed).
    """
    rows: list[dict] = []
    for group in (groups_payload or {}).get("groups", []) or []:
        group_name = group.get("name") or ""
        for team in group.get("teams", []) or []:
            team_id = team.get("team_id")
            if team_id is None:
                continue
            team_id = str(team_id)
            ident = team_index.get(team_id)
            if ident is None:
                logger.warning("worldcup standings: team_id %s not in index", team_id)
                continue
            rows.append(
                {
                    "team_id": team_id,
                    "name": ident["name"],
                    "fifa_code": ident["fifa_code"],
                    "flag_url": ident["flag"],
                    "group_name": group_name,
                    "mp": _to_int(team.get("mp")),
                    "w": _to_int(team.get("w")),
                    "d": _to_int(team.get("d")),
                    "l": _to_int(team.get("l")),
                    "pts": _to_int(team.get("pts")),
                    "gf": _to_int(team.get("gf")),
                    "ga": _to_int(team.get("ga")),
                    "gd": _to_int(team.get("gd")),
                }
            )
    return rows


def _parse_kickoff(local_date: str | None) -> datetime | None:
    """Parse a "MM/DD/YYYY HH:MM" venue-local string to an aware UTC datetime.

    The feed has no timezone offset, so we attach US Eastern (see _VENUE_TZ) and
    convert to UTC. Returns None on any parse failure (never raises).
    """
    if not local_date:
        return None
    try:
        naive = datetime.strptime(local_date.strip(), _LOCAL_DATE_FMT)
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=_VENUE_TZ).astimezone(timezone.utc)


def parse_fixtures(games_payload: dict, team_index: dict) -> list[dict]:
    """Parse /get/games into group-stage fixtures joined to team_index for codes.

    Only ``type == "group"`` games are kept (knockout/other dropped). FIFA codes
    are recovered from team_index by home/away team_id (the games carry names but
    no codes, and the sim joins on FIFA code). ``finished`` is the literal string
    "TRUE"/"FALSE"; scores are placeholders until finished, so they are None
    unless finished. kickoff is the Eastern->UTC conversion of local_date.
    """
    fixtures: list[dict] = []
    for game in (games_payload or {}).get("games", []) or []:
        if game.get("type") != "group":
            continue

        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")
        home_id = str(home_id) if home_id is not None else None
        away_id = str(away_id) if away_id is not None else None

        home_ident = team_index.get(home_id) if home_id else None
        away_ident = team_index.get(away_id) if away_id else None
        home_code = home_ident["fifa_code"] if home_ident else None
        away_code = away_ident["fifa_code"] if away_ident else None
        if not home_id or not away_id or home_code is None or away_code is None:
            logger.warning(
                "worldcup fixtures: unresolved code for game %s (home=%s away=%s), dropping",
                game.get("id"),
                home_id,
                away_id,
            )
            continue

        finished = (game.get("finished") or "").upper() == "TRUE"
        home_score = _to_int(game.get("home_score")) if finished else None
        away_score = _to_int(game.get("away_score")) if finished else None

        home_name = game.get("home_team_name_en") or (
            home_ident["name"] if home_ident else ""
        )
        away_name = game.get("away_team_name_en") or (
            away_ident["name"] if away_ident else ""
        )

        fixtures.append(
            {
                "match_id": str(game.get("id")) if game.get("id") is not None else "",
                "group_name": game.get("group") or "",
                "matchday": _to_int(game.get("matchday")),
                "home_id": home_id,
                "home_name": home_name,
                "home_code": home_code,
                "away_id": away_id,
                "away_name": away_name,
                "away_code": away_code,
                "home_score": home_score,
                "away_score": away_score,
                "finished": finished,
                "kickoff": _parse_kickoff(game.get("local_date")),
            }
        )
    return fixtures


def _is_retryable(exc: Exception) -> bool:
    """Retry transient failures only. A 4xx is a client error that will not fix
    itself on retry; 5xx, timeouts, and connection errors are transient."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return status >= 500
    # No response (timeout, connect error, read error) -> transient.
    return isinstance(exc, httpx.HTTPError)


async def _get_json(client: httpx.AsyncClient, path: str, stats: dict) -> dict:
    """GET a path off the client's base_url and decode JSON. Never raises.

    Retries transient upstream failures (5xx, timeouts, connection errors) with
    exponential backoff so a single intermittent blip on one endpoint does not
    abort the whole fetch. After the final attempt (or on a non-retryable 4xx)
    it records the failure into stats["errors"] and returns {} so the caller
    degrades gracefully.
    """
    last_exc: Exception | None = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            resp = await client.get(path, timeout=_TIMEOUT_SECS)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == _FETCH_ATTEMPTS - 1:
                break
            backoff = _FETCH_BACKOFF_SECS * (2**attempt)
            logger.warning(
                "worldcup fetch %s attempt %d/%d failed (%s); retrying in %.1fs",
                path,
                attempt + 1,
                _FETCH_ATTEMPTS,
                exc,
                backoff,
            )
            await asyncio.sleep(backoff)
    logger.error("worldcup fetch failed for %s: %s", path, last_exc)
    stats["errors"].append(f"{path}: {last_exc}")
    return {}


async def fetch_all(
    client: httpx.AsyncClient,
) -> tuple[list[dict], list[dict], dict]:
    """Fetch all three endpoints, parse, and return (standings, fixtures, stats).

    The base URL comes from the passed AsyncClient (configured by the caller from
    WORLDCUP_API_BASE, default https://worldcup26.ir). Never raises: HTTP and
    parse errors are caught and recorded in stats["errors"], and whatever parsed
    successfully is returned. This does NO database writes.
    """
    stats: dict = {
        "teams": 0,
        "standings": 0,
        "fixtures": 0,
        "finished": 0,
        "errors": [],
    }

    teams_payload = await _get_json(client, "/get/teams", stats)
    groups_payload = await _get_json(client, "/get/groups", stats)
    games_payload = await _get_json(client, "/get/games", stats)

    team_index = build_team_index(teams_payload)
    stats["teams"] = len(team_index)

    standings_rows: list[dict] = []
    fixture_rows: list[dict] = []
    try:
        standings_rows = parse_standings(groups_payload, team_index)
    except Exception as exc:  # defensive: a malformed payload must not abort
        logger.error("worldcup standings parse failed: %s", exc)
        stats["errors"].append(f"standings parse: {exc}")
    try:
        fixture_rows = parse_fixtures(games_payload, team_index)
    except Exception as exc:  # defensive: a malformed payload must not abort
        logger.error("worldcup fixtures parse failed: %s", exc)
        stats["errors"].append(f"fixtures parse: {exc}")

    stats["standings"] = len(standings_rows)
    stats["fixtures"] = len(fixture_rows)
    stats["finished"] = sum(1 for f in fixture_rows if f["finished"])
    return standings_rows, fixture_rows, stats
