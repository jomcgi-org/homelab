"""Ships: in-monolith AIS vessel tracking (migrated from the standalone marine app)."""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register ships routers with the app."""
    from ships.router import router

    app.include_router(router)


def on_startup_jobs(session: Session) -> None:
    """Register ships scheduled jobs (partition maintenance / retention)."""
    from shared.scheduler import register_job
    from ships.retention import partition_maintenance_handler

    register_job(
        session,
        name="ships.partition_maintenance",
        interval_secs=86400,
        handler=partition_maintenance_handler,
        ttl_secs=3600,
    )
