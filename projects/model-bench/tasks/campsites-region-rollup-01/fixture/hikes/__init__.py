"""Hikes: in-monolith Scottish walk planner (migrated from the standalone hikes app)."""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register hikes routers with the app."""
    from hikes.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Hikes is a wholly public, read-only domain: reuse register."""
    register(app)


def on_startup_jobs(session: Session) -> None:
    """Register hikes scheduled jobs (scrape + forecast refresh + hourly prune)."""
    from scheduler.api import register_job
    from hikes.jobs import (
        prune_windows_handler,
        refresh_forecasts_handler,
        scrape_walks_handler,
    )

    register_job(
        session,
        name="hikes.scrape_walks",
        interval_secs=7 * 86400,  # weekly; the walk corpus barely changes
        handler=scrape_walks_handler,
        ttl_secs=3 * 3600,  # polite full scrape can take a while
    )
    register_job(
        session,
        name="hikes.refresh_forecasts",
        # Every 2 h: met.no's multi-day forecast only re-runs a few times a day,
        # so this hedges to catch a fresh model run within ~2 h without hammering
        # met.no (hourly would re-fetch identical data ~20x/day for ~1600 walks).
        interval_secs=2 * 3600,
        handler=refresh_forecasts_handler,
        ttl_secs=1800,
    )
    register_job(
        session,
        name="hikes.prune_windows",
        # Hourly housekeeping: drop walk_hours whose clock hour has elapsed so
        # the table does not grow unbounded between the slower refreshes. The
        # read endpoints filter defensively regardless (top_of_hour cutoff).
        interval_secs=3600,
        handler=prune_windows_handler,
        ttl_secs=300,
    )
