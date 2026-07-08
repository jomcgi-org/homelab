"""Ships: in-monolith AIS vessel tracking (migrated from the standalone marine app)."""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register ships routers with the app."""
    from ships.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Ships is a wholly public, read-only domain: reuse register."""
    register(app)


def on_startup_jobs(session: Session) -> None:
    """Register ships scheduled jobs (partition maintenance, heatmap rollup)."""
    from scheduler.api import register_job
    from ships.heat import heat_rollup_handler
    from ships.retention import partition_maintenance_handler

    register_job(
        session,
        name="ships.partition_maintenance",
        interval_secs=86400,
        handler=partition_maintenance_handler,
        ttl_secs=3600,
    )
    register_job(
        session,
        name="ships.heat_rollup",
        interval_secs=3600,
        handler=heat_rollup_handler,
        ttl_secs=1800,
    )
