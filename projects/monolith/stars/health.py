"""Cheap health signals for the stars public read surface.

This check deliberately does not call the sites or history endpoint. Those
routes materialize a large response, while health is called frequently and
must remain safe under load. Aggregate SQL still detects an empty or stale
forecast, which is the dependency that makes the stars page useful.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, func, select

from shared.forecast_freshness import top_of_hour
from stars.models import Site, SiteHour

# Refresh runs every three hours. Allow one missed run plus startup and queue
# headroom before declaring the forecast stale.
FORECAST_MAX_AGE_SECONDS = float(
    os.environ.get("STARS_HEALTH_MAX_AGE_SECONDS", str(6 * 60 * 60))
)


@dataclass(frozen=True)
class StarsHealthSnapshot:
    site_count: int
    future_forecast_rows: int
    latest_fetched_at: datetime | None


def _read_snapshot() -> StarsHealthSnapshot:
    """Read bounded health facts without hydrating Site or SiteHour objects."""
    from core.db import get_engine

    cutoff = top_of_hour()
    with Session(get_engine()) as session:
        site_count = session.exec(select(func.count()).select_from(Site)).one()
        future_forecast_rows = session.exec(
            select(func.count())
            .select_from(SiteHour)
            .where(SiteHour.hour_time >= cutoff)
        ).one()
        latest_fetched_at = session.exec(
            select(func.max(SiteHour.fetched_at)).where(SiteHour.hour_time >= cutoff)
        ).one()
    return StarsHealthSnapshot(
        site_count=int(site_count),
        future_forecast_rows=int(future_forecast_rows),
        latest_fetched_at=latest_fetched_at,
    )


async def stars_health() -> dict:
    """Report whether the stars data dependency can serve a useful page."""
    try:
        snapshot = await asyncio.to_thread(_read_snapshot)
    except Exception as exc:  # noqa: BLE001 - framework records the detail
        return {"ok": False, "detail": f"stars data check failed: {exc}"}

    if snapshot.site_count == 0:
        return {"ok": False, "detail": "stars.sites is empty"}
    if snapshot.future_forecast_rows == 0:
        return {"ok": False, "detail": "no future stars forecast rows"}
    if snapshot.latest_fetched_at is None:
        return {"ok": False, "detail": "stars forecast has no fetch timestamp"}

    latest = snapshot.latest_fetched_at
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (datetime.now(timezone.utc) - latest).total_seconds())
    if age_seconds > FORECAST_MAX_AGE_SECONDS:
        return {
            "ok": False,
            "detail": f"stars forecast is {age_seconds / 3600:.1f}h old",
        }

    return {
        "ok": True,
        "detail": (
            f"{snapshot.site_count} sites, "
            f"{snapshot.future_forecast_rows} future rows, "
            f"fetched {age_seconds / 60:.0f}m ago"
        ),
    }
