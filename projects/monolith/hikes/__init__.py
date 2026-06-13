"""Hikes: in-monolith Scottish walk planner (migrated from the standalone hikes app)."""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register hikes routers with the app."""
    from hikes.router import router

    app.include_router(router)


def on_startup_jobs(session: Session) -> None:
    """Register hikes scheduled jobs (scrape + forecast refresh)."""
    from shared.scheduler import register_job
    from hikes.jobs import refresh_forecasts_handler, scrape_walks_handler

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
