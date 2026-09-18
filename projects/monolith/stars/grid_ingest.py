"""Transactional ingest contract shared by stars grid producers and loaders."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from stars.models import Site, SiteHour

logger = logging.getLogger("monolith.stars.grid")


def _site_rows(grid: list[dict]) -> tuple[list[Site], int]:
    now = datetime.now(timezone.utc)
    rows: list[Site] = []
    skipped = 0
    for point in grid:
        if not isinstance(point, dict):
            skipped += 1
            continue
        site_id = point.get("id")
        lat = point.get("lat")
        lon = point.get("lon")
        if site_id is None or lat is None or lon is None:
            skipped += 1
            continue
        rows.append(
            Site(
                id=str(site_id),
                name=point.get("name"),
                lat=float(lat),
                lon=float(lon),
                altitude_m=int(point.get("altitude_m") or 0),
                lp_zone=str(point.get("lp_zone") or "unknown"),
                source="grid",
                updated_at=now,
            )
        )
    return rows, skipped


def replace_grid(grid: list[dict], *, engine=None, reject_invalid: bool = False) -> int:
    """Replace ``stars.sites`` atomically from a grid-compatible site list.

    The scheduled object loader retains its historical behavior of skipping
    malformed entries. Dedicated producers set ``reject_invalid`` so output
    incompatibility fails before the database transaction starts.
    """
    rows, skipped = _site_rows(grid)
    if skipped and reject_invalid:
        raise ValueError(f"grid contains {skipped} malformed site rows")
    if skipped:
        logger.warning("stars.load_grid: skipped %d malformed grid points", skipped)
    if not rows:
        logger.warning("stars.load_grid: no valid grid points, leaving table intact")
        return 0

    if engine is None:
        from core.db import get_engine

        engine = get_engine()

    with Session(engine) as session:
        session.execute(delete(Site))
        session.add_all(rows)
        # add_all autoflushes before the subquery, so it sees the replacement
        # grid. Seasonal climatology rows remain because their bounded history
        # is still useful if a site later returns to the grid.
        session.execute(
            delete(SiteHour)
            .where(SiteHour.site_id.notin_(select(Site.id)))
            .execution_options(synchronize_session=False)
        )
        session.commit()
    return len(rows)


def replace_computed_grid(grid: list[dict], *, engine=None) -> int:
    """Strict ingest contract for freshly computed grid output."""
    rows, skipped = _site_rows(grid)
    if skipped:
        raise ValueError(f"grid contains {skipped} malformed site rows")
    site_ids = [row.id for row in rows]
    if len(site_ids) != len(set(site_ids)):
        raise ValueError("grid contains duplicate site ids")
    return replace_grid(grid, engine=engine, reject_invalid=True)
