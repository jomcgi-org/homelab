"""Stars scheduled job handlers (refresh + prune).

refresh_handler runs the met.no fetch + astronomy scoring for every seed site
and wholesale-replaces that site's site_hours rows. prune_hours_handler drops
hours once their clock hour has elapsed. Both follow the scheduler contract
(async def returning ``datetime | None``) and the network-before-session
ordering used by hikes.jobs: the whole network phase runs first via httpx
async, then the synchronous SQLModel I/O runs inline in the same coroutine.
The Session is never handed to a worker thread (no-session-in-to-thread).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session, delete

from stars.forecast import fetch_all
from stars.models import SiteHour
from stars.seed import SCOTLAND_DARK_SKY_LOCATIONS

logger = logging.getLogger("monolith.stars.jobs")

_BY_ID = {loc["id"]: loc for loc in SCOTLAND_DARK_SKY_LOCATIONS}


async def refresh_handler(session: Session) -> datetime | None:
    """Refresh per-site dark-hour forecasts from met.no.

    Fetch + score every seed site first (no session work during the network
    phase). Then, for each site that fetched successfully, wholesale-replace its
    rows: delete the site's existing hours and insert one row per scored hour.
    Sites whose fetch failed are absent from ``scored`` and keep their previous
    rows (stale beats empty). A total fetch failure writes nothing.
    """
    scored = await fetch_all()
    if not scored:
        logger.warning("stars.refresh: empty fetch, keeping existing rows")
        return None

    fetched_at = datetime.now(timezone.utc)
    for site_id, hours in scored.items():
        session.execute(delete(SiteHour).where(SiteHour.site_id == site_id))
        for h in hours:
            hour_time = datetime.fromisoformat(h["time"].replace("Z", "+00:00"))
            session.add(
                SiteHour(
                    site_id=site_id,
                    hour_time=hour_time,
                    score=h["score"],
                    cloud_area_fraction=h["cloud_area_fraction"],
                    relative_humidity=h["relative_humidity"],
                    wind_speed=h["wind_speed"],
                    air_temperature=h["air_temperature"],
                    dew_spread=h["dew_spread"],
                    symbol=h["symbol"],
                    fetched_at=fetched_at,
                )
            )
    session.commit()
    logger.info(
        "stars.refresh ok: refreshed %d/%d sites",
        len(scored),
        len(SCOTLAND_DARK_SKY_LOCATIONS),
    )
    return None


async def prune_hours_handler(
    session: Session,
) -> datetime | None:  # implemented in a later task
    raise NotImplementedError
