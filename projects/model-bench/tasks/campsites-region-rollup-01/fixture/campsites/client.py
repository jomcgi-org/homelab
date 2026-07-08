"""BC Parks GoingToCamp reservation API client.

Talks to camping.bcparks.ca (Azure WAF-protected) to fetch nightly
availability. The WAF fingerprints the TLS handshake (JA3), so httpx and urllib
get a 403 even with perfect browser headers: only a browser-TLS-impersonating
client passes. We use curl_cffi with impersonate="chrome". The HEADERS dict is
still sent on every call as defence in depth.

The campground catalog is STATIC (committed at campsites/catalog.json, generated
offline by gen_catalog.py); load_catalog() reads it at runtime rather than
scraping it live. This module has NO database writes. It is used only by
campsites.jobs (the hourly refresh job) and is excluded from the public binary.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path

from curl_cffi.requests import AsyncSession

logger = logging.getLogger("monolith.campsites")

BASE = "https://camping.bcparks.ca"

# Browser-like UA required to pass the Azure WAF. The trailing tag identifies
# the tool and a contact URL so the operator can reach us if needed.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 "
    "jomcgi-campsites/1.0 (+https://jomcgi.dev/app/campsites)"
)

HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": "https://camping.bcparks.ca/",
    "Origin": "https://camping.bcparks.ca",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

EQUIPMENT_ANY: int = -32768
WINDOW_DAYS: int = 14
INTER_REQUEST_DELAY_S: float = 0.4
MAX_CONSECUTIVE_FAILURES: int = 5


@dataclass
class CampgroundRow:
    """Parsed campground record from the GoingToCamp catalog."""

    resource_location_id: int
    park_map_id: int
    name: str
    region: str
    latitude: float
    longitude: float
    iana_tz: str
    description: str
    booking_url: str


@dataclass
class DayAvail:
    """Aggregated availability for one campground on one date."""

    date: datetime.date
    has_availability: bool
    loops_open: int


# ---------------------------------------------------------------------------
# Static catalog loader
# ---------------------------------------------------------------------------

BOOKING_ROOT = "https://camping.bcparks.ca/"


def _read_catalog_json() -> list[dict]:
    """Locate and parse the packaged catalog.json.

    Prefers importlib.resources (robust under Bazel runfiles, where the file
    ships as a data dependency next to this package) and falls back to the
    filesystem path beside this module for a plain source checkout.
    """
    try:
        from importlib.resources import files

        text = files("campsites").joinpath("catalog.json").read_text(encoding="utf-8")
    except Exception:
        logger.debug(
            "campsites: importlib.resources catalog lookup failed, "
            "falling back to the module-relative path",
            exc_info=True,
        )
        text = (Path(__file__).parent / "catalog.json").read_text(encoding="utf-8")
    return json.loads(text)


def load_catalog() -> list[CampgroundRow]:
    """Load the committed static campground catalog as CampgroundRow rows.

    Reads campsites/catalog.json (generated offline by gen_catalog.py). The
    runtime job does NOT scrape the catalog; it only fetches availability and
    weather live each hour. Rows with a null latitude or longitude are skipped
    and logged at WARNING: the map cannot plot them and the weather join needs
    coordinates. booking_url is the park's bcparks.ca website when present, else
    the GoingToCamp reservation root.
    """
    rows: list[CampgroundRow] = []
    for entry in _read_catalog_json():
        lat = entry.get("latitude")
        lon = entry.get("longitude")
        if lat is None or lon is None:
            logger.warning(
                "campsites catalog: park %s (%s) has no coordinates, skipping",
                entry.get("resource_location_id"),
                entry.get("name"),
            )
            continue
        website = entry.get("website") or ""
        rows.append(
            CampgroundRow(
                resource_location_id=int(entry["resource_location_id"]),
                park_map_id=int(entry["park_map_id"]),
                name=entry.get("name") or "",
                region=entry.get("region") or "",
                latitude=float(lat),
                longitude=float(lon),
                iana_tz=entry.get("iana_tz") or "America/Vancouver",
                description=entry.get("description") or "",
                booking_url=website or BOOKING_ROOT,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Pure availability parser (unit-tested)
# ---------------------------------------------------------------------------


def merge_availability(
    payload: dict,
    start_date: datetime.date,
    ndays: int = WINDOW_DAYS,
) -> list[DayAvail]:
    """Aggregate per-loop availability codes into one DayAvail per date.

    ``payload["mapLinkAvailabilities"]`` maps loop map IDs (str keys) to lists
    of GoingToCamp availability status codes, one per date starting at
    ``start_date``. The code is an enum, NOT a boolean:

        0 = available (has bookable sites)
        1 = unavailable / fully booked
        2 = closed / not operating

    So a loop is open on a date only when its code is exactly ``0``. (An earlier
    version treated any truthy value as open, which inverted the meaning and
    reported booked (1) and closed (2) loops as available: full parks showed as
    "open". Confirmed against the live API, where Gordon Bay returned loops of
    all-1 and all-2 that were being counted as bookable.)

    A date has availability if any loop is ``0`` at that index. loops_open is
    the count of loops that are ``0`` at that index. Ragged arrays are safe: a
    missing index contributes nothing (treated as not available). An empty or
    missing payload produces ``ndays`` closed DayAvail rows.
    """
    arrays = list((payload.get("mapLinkAvailabilities") or {}).values())
    result: list[DayAvail] = []
    for i in range(ndays):
        has_avail = False
        loops_open = 0
        for arr in arrays:
            if not arr or i >= len(arr):
                continue
            if arr[i] == 0:
                has_avail = True
                loops_open += 1
        result.append(
            DayAvail(
                date=start_date + datetime.timedelta(days=i),
                has_availability=has_avail,
                loops_open=loops_open,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Async network functions (caller passes a curl_cffi AsyncSession)
#
# The session MUST be created with impersonate="chrome" (or another browser
# profile). That is what defeats the Azure WAF's JA3 TLS fingerprint check;
# httpx and urllib get a 403 regardless of headers. HEADERS is still passed on
# every request as defence in depth.
# ---------------------------------------------------------------------------


def _park_local_today(iana_tz: str) -> datetime.date:
    """Return today's date in the park's local timezone, falling back to UTC.

    GoingToCamp interprets the startDate param in the park's local time, so
    anchoring to the park's local date keeps availability and weather on the
    same per-date grid. Falls back gracefully to UTC if zoneinfo cannot resolve
    the timezone identifier (logs a warning; does NOT crash the job).
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(iana_tz)).date()
    except Exception:
        logger.warning(
            "campsites: tz %s unresolved, falling back to UTC date",
            iana_tz,
            exc_info=True,
        )
        return datetime.datetime.now(datetime.timezone.utc).date()


async def fetch_availability(
    session: AsyncSession,
    cg: CampgroundRow,
    start_date: datetime.date,
) -> list[DayAvail] | None:
    """GET availability for one campground over WINDOW_DAYS nights.

    Returns None on any error (non-2xx response or network exception), logged
    at WARNING level, so the caller can skip this park and continue with others.
    """
    end_date = start_date + datetime.timedelta(days=WINDOW_DAYS)
    params = {
        "mapId": cg.park_map_id,
        "resourceLocationId": cg.resource_location_id,
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "nights": 1,
        "getDailyAvailability": "true",
        "equipmentCategoryId": EQUIPMENT_ANY,
        "partySize": 4,
    }
    try:
        resp = await session.get(
            f"{BASE}/api/availability/map",
            params=params,
            headers=HEADERS,
            timeout=25,
        )
        resp.raise_for_status()
        return merge_availability(resp.json(), start_date)
    except Exception:
        logger.warning(
            "campsites: availability fetch failed for park %d",
            cg.resource_location_id,
            exc_info=True,
        )
        return None


async def fetch_all_availability(
    session: AsyncSession,
    cats: list[CampgroundRow],
) -> dict[int, list[DayAvail]]:
    """Fetch availability for all campgrounds sequentially with a politeness delay.

    For each park, the availability window is anchored to that park's local
    today (via _park_local_today) so the returned DayAvail dates align with the
    park-local dates that Open-Meteo returns for the same timezone. GoingToCamp
    interprets startDate in park-local time, so using UTC-today during BC
    evenings (UTC 00:00-07:00) would shift the grid one day forward.

    Between parks, waits INTER_REQUEST_DELAY_S plus a deterministic per-index
    jitter (random.Random(i).uniform(0, 0.3)) so behaviour is reproducible and
    wall-clock randomness cannot affect test runs. On failure, retries once
    after 2 seconds. If MAX_CONSECUTIVE_FAILURES parks fail in a row, logs an
    error and returns what has been collected so far rather than continuing to
    hammer a WAF that is likely rate-limiting us.
    """
    results: dict[int, list[DayAvail]] = {}
    consecutive_failures = 0

    for i, cg in enumerate(cats):
        if i > 0:
            jitter = random.Random(i).uniform(0, 0.3)
            await asyncio.sleep(INTER_REQUEST_DELAY_S + jitter)

        local_start = _park_local_today(cg.iana_tz)
        avail = await fetch_availability(session, cg, local_start)

        if avail is None:
            await asyncio.sleep(2.0)
            avail = await fetch_availability(session, cg, local_start)

        if avail is None:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error(
                    "campsites: %d consecutive failures reaching the API; "
                    "WAF likely blocking, stopping run early to avoid an IP ban",
                    consecutive_failures,
                )
                break
        else:
            consecutive_failures = 0
            results[cg.resource_location_id] = avail

    return results
