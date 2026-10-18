"""Home domain: powers the homepage dashboard (schedule, stats)."""

from fastapi import FastAPI


def register(app: FastAPI) -> None:
    from home.schedule_router import router as schedule_router
    from home.observability.router import router as observability_router
    from home.dashboard_router import router as dashboard_router

    app.include_router(schedule_router)
    app.include_router(observability_router)
    app.include_router(dashboard_router)


def register_public(app: FastAPI) -> None:
    """Register only the public, read-only home routes (observability stats)."""
    from home.observability.router import router as observability_router

    app.include_router(observability_router)


def get_today_events(session) -> list[dict]:
    """Home domain public API: today's calendar events from the snapshot row."""
    from home.schedule import get_today_events as _get

    return _get(session)
