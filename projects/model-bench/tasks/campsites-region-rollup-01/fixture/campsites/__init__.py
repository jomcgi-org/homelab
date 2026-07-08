"""campsites: BC Parks availability x clear-sky weather (/app/campsites)."""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register the campsites router (read-only, public-safe)."""
    from campsites.router import router

    app.include_router(router)


register_public = register  # wholly public, read-only


def on_startup_jobs(session: Session) -> None:
    """Register the hourly refresh job (no-op when Argo owns it)."""
    from scheduler.api import argo_handled, register_job

    if argo_handled("campsites.refresh"):
        return
    from campsites.jobs import refresh_handler

    register_job(
        session,
        name="campsites.refresh",
        interval_secs=3600,
        handler=refresh_handler,
        ttl_secs=600,
    )
