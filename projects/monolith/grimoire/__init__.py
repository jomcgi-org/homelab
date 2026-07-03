"""Grimoire: D&D campaign manager, private-tier monolith domain module.

Follows ADR 010 (privilege-typed module) and ADR 011 (hot-tier schema). No
register_public: Grimoire is private-tier only in v1.
"""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register the grimoire router with the app (private tier only)."""
    from grimoire.router import router

    app.include_router(router)


def on_startup_jobs(session: Session) -> None:
    """Register grimoire scheduled jobs. No-op until the ingest jobs land."""
