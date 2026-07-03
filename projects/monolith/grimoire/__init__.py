"""Grimoire: D&D campaign manager, private-tier monolith domain module.

Follows ADR 010 (privilege-typed module) and ADR 011 (hot-tier schema). No
register_public: Grimoire is private-tier only in v1.
"""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register grimoire routers with the app. No-op until the router lands."""


def on_startup_jobs(session: Session) -> None:
    """Register grimoire scheduled jobs. No-op until the ingest jobs land."""
