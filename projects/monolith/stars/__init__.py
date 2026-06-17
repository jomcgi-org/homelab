"""Stars domain: best dark-sky viewing windows for grid-sourced sites.

The site list lives in stars.sites, populated by the stars.load_grid job from a
light-pollution grid (grid.json) uploaded to SeaweedFS out-of-band (ADR 006). A
scheduled refresh job fetches MET Norway forecasts for each site, scores each
dark hour, and stores all future qualifying hours in Postgres
(stars.site_hours). An hourly prune drops elapsed hours; the read endpoint
filters defensively and is the source of truth.
"""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    from stars.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Stars is a wholly public, read-only domain: reuse register."""
    register(app)


def on_startup_jobs(session: Session) -> None:
    from scheduler.api import register_job
    from stars.grid import load_climatology_handler, load_grid_handler
    from stars.jobs import prune_hours_handler, refresh_handler

    register_job(
        session,
        name="stars.load_grid",
        interval_secs=6 * 3600,
        handler=load_grid_handler,
        ttl_secs=600,
    )
    register_job(
        session,
        name="stars.load_climatology",
        interval_secs=24 * 3600,
        handler=load_climatology_handler,
        ttl_secs=600,
    )
    register_job(
        session,
        name="stars.refresh",
        interval_secs=3 * 3600,
        handler=refresh_handler,
        ttl_secs=900,
    )
    register_job(
        session,
        name="stars.prune_hours",
        interval_secs=3600,
        handler=prune_hours_handler,
        ttl_secs=300,
    )
