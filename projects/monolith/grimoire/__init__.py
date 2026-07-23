"""Grimoire: D&D campaign manager, private-tier monolith domain module.

Follows ADR 010 (privilege-typed module) and ADR 011 (hot-tier schema).
register_public exposes a corpus-global, no-grants read surface for the
public tier (Task 2 of the public-readonly design);
campaign/grant CRUD stays private-tier only.
"""

from fastapi import FastAPI
from sqlmodel import Session


def register(app: FastAPI) -> None:
    """Register the grimoire router with the app (private tier only)."""
    from grimoire.router import router

    app.include_router(router)


def register_public(app: FastAPI) -> None:
    """Register only the public, read-only Grimoire routes (no campaign/grant
    surface, see grimoire/router_public.py)."""
    from grimoire.router_public import router as public_router

    app.include_router(public_router)


def on_startup_jobs(session: Session) -> None:
    """Register grimoire ingest jobs (spec #4.2): a daily chunk loader and a
    daily heavy entity-extraction pass.

    Both are idempotent batch jobs; ``register_job`` skips any that an Argo
    CronWorkflow owns. Extraction is flagged ``heavy`` (LLM calls, long-running)
    so the dispatcher never co-schedules it with another memory-heavy job, and
    gets a generous 25m deadline; the loader gets 10m.
    """
    from scheduler.api import register_job

    from grimoire.jobs import grimoire_extract_entities, grimoire_load_chunks

    register_job(
        session,
        name="grimoire.load_chunks",
        interval_secs=86_400,
        handler=grimoire_load_chunks,
        ttl_secs=600,
    )
    register_job(
        session,
        name="grimoire.extract_entities",
        interval_secs=86_400,
        handler=grimoire_extract_entities,
        ttl_secs=1_500,
        heavy=True,
    )
