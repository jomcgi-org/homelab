"""BC Parks GoingToCamp reservation API client.

Talks to camping.bcparks.ca (Azure WAF-protected) to fetch campground catalog
and nightly availability. The WAF passes browser-like requests; every call
must send the HEADERS dict defined below or the server returns 403.

This module has NO database writes. It is used only by campsites.jobs (the
hourly refresh job). It is excluded from the public binary.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import random
from dataclasses import dataclass

import httpx

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
# Helpers (pure, no network)
# ---------------------------------------------------------------------------


def _extract_gps(entry: dict) -> tuple[float, float] | None:
    """Extract (latitude, longitude) from a resourceLocation entry.

    The API has been observed to serve gpsCoordinates in at least two shapes:
      flat:   {"latitude": 49.x, "longitude": -119.x}
      nested: {"point": {"latitude": 49.x, "longitude": -119.x}}

    We try the flat shape first, then probe any nested dict values as a
    fallback. Returns None when coordinates are absent or non-numeric.
    """
    gps = entry.get("gpsCoordinates")
    if not gps:
        return None

    lat = gps.get("latitude")
    lon = gps.get("longitude")

    if lat is None or lon is None:
        for v in gps.values():
            if isinstance(v, dict):
                lat = v.get("latitude")
                lon = v.get("longitude")
                if lat is not None and lon is not None:
                    break

    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _pick_localized(values: list, culture: str = "en-CA") -> dict:
    """Return the localized entry for ``culture``, or the first entry if absent."""
    if not values:
        return {}
    for v in values:
        if v.get("cultureName") == culture:
            return v
    return values[0]


# ---------------------------------------------------------------------------
# Pure catalog + availability parsers (unit-tested)
# ---------------------------------------------------------------------------


def parse_catalog(resource_json: list, maps_json: list) -> list[CampgroundRow]:
    """Join maps links to resourceLocation entries and return CampgroundRows.

    Builds a resourceLocationId -> (childMapId, map_title) index from maps_json,
    then iterates resource_json to produce rows. Entries with no matching
    park_map_id or no GPS coordinates are dropped and logged at DEBUG level.
    If one resourceLocationId appears in multiple mapLinks, the first is kept.

    Name resolution order: en-CA shortName -> en-CA fullName -> map link title.
    booking_url is BASE + "/" for now (a later task may refine to per-park URLs).
    """
    # Build resourceLocationId -> (childMapId, title) from the maps tree.
    map_index: dict[int, tuple[int, str]] = {}
    for top in maps_json or []:
        for link in top.get("mapLinks", []) or []:
            rid = link.get("resourceLocationId")
            child_map_id = link.get("childMapId")
            if rid is None or child_map_id is None:
                continue
            rid = int(rid)
            if rid in map_index:
                continue  # keep the first link per location
            locs = link.get("localizations") or []
            loc = _pick_localized(locs)
            title = loc.get("title") or ""
            map_index[rid] = (int(child_map_id), title)

    rows: list[CampgroundRow] = []
    for entry in resource_json or []:
        rid = entry.get("resourceLocationId")
        if rid is None:
            continue
        rid = int(rid)

        if rid not in map_index:
            logger.debug(
                "campsites catalog: resourceLocationId %d has no map link, skipping",
                rid,
            )
            continue

        park_map_id, map_title = map_index[rid]

        gps = _extract_gps(entry)
        if gps is None:
            logger.debug(
                "campsites catalog: resourceLocationId %d has no GPS coords, skipping",
                rid,
            )
            continue

        lat, lon = gps
        loc_vals = entry.get("localizedValues") or []
        loc = _pick_localized(loc_vals)
        short_name = loc.get("shortName") or ""
        full_name = loc.get("fullName") or ""
        name = short_name or full_name or map_title or ""
        description = loc.get("description") or ""
        region = entry.get("region") or ""
        iana_tz = entry.get("ianaTimeZone") or "America/Vancouver"

        rows.append(
            CampgroundRow(
                resource_location_id=rid,
                park_map_id=park_map_id,
                name=name,
                region=region,
                latitude=lat,
                longitude=lon,
                iana_tz=iana_tz,
                description=description,
                booking_url=BASE + "/",
            )
        )

    return rows


def merge_availability(
    payload: dict,
    start_date: datetime.date,
    ndays: int = WINDOW_DAYS,
) -> list[DayAvail]:
    """Aggregate per-loop availability flags into one DayAvail per date.

    ``payload["mapLinkAvailabilities"]`` maps loop map IDs (str keys) to lists
    of truthy/falsy flags, one per date starting at ``start_date``. A date has
    availability if any loop has a truthy value at that index. loops_open is
    the count of loops that have a truthy value at that index. Ragged arrays
    are safe: a missing index is treated as 0 (closed). An empty or missing
    payload produces ``ndays`` closed DayAvail rows.
    """
    arrays = list((payload.get("mapLinkAvailabilities") or {}).values())
    result: list[DayAvail] = []
    for i in range(ndays):
        has_avail = False
        loops_open = 0
        for arr in arrays:
            if not arr or i >= len(arr):
                continue
            if arr[i]:
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
# Async network functions (caller passes an httpx.AsyncClient)
# ---------------------------------------------------------------------------


async def fetch_catalog(client: httpx.AsyncClient) -> list[CampgroundRow]:
    """GET the resourceLocation and maps catalogs, join, and return CampgroundRows.

    The client must be configured with HEADERS (so the WAF passes the request).
    Returns an empty list on any network or parse error (logged at ERROR).
    """
    try:
        r_res = await client.get(f"{BASE}/api/resourceLocation")
        r_res.raise_for_status()
        resource_json = r_res.json()

        r_maps = await client.get(f"{BASE}/api/maps")
        r_maps.raise_for_status()
        maps_json = r_maps.json()

        return parse_catalog(resource_json, maps_json)
    except Exception:
        logger.error("campsites: catalog fetch failed", exc_info=True)
        return []


async def fetch_availability(
    client: httpx.AsyncClient,
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
        resp = await client.get(f"{BASE}/api/availability/map", params=params)
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
    client: httpx.AsyncClient,
    cats: list[CampgroundRow],
    start_date: datetime.date,
) -> dict[int, list[DayAvail]]:
    """Fetch availability for all campgrounds sequentially with a politeness delay.

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

        avail = await fetch_availability(client, cg, start_date)

        if avail is None:
            await asyncio.sleep(2.0)
            avail = await fetch_availability(client, cg, start_date)

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
