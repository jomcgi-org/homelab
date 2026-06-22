"""Home domain — powers the homepage dashboard (schedule, topology, stats)."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from home.schedule_router import router as schedule_router
    from home.observability.router import router as observability_router

    app.include_router(schedule_router)
    app.include_router(observability_router)


def register_public(app: FastAPI) -> None:
    """Register only the public, read-only home routes (observability stats/topology)."""
    from home.observability.router import router as observability_router

    app.include_router(observability_router)


def on_startup_jobs(session) -> None:
    from scheduler.api import register_job
    from home.schedule import calendar_poll_handler

    register_job(
        session,
        name="home.calendar_poll",
        interval_secs=900,
        handler=lambda _: calendar_poll_handler(),
        ttl_secs=120,
    )
