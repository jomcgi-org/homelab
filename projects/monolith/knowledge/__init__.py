"""Knowledge domain: knowledge graph CRUD, search, tasks, and dead-letter management.

The cross-domain public surface lives in ``knowledge.api``; its callables are
re-exported here so the public-function coverage contract (see
``app/bdd_completeness_test.py``) keeps tracking them under their
``knowledge.*`` names. Other domains must import from ``knowledge.api``
(enforced by ``import_boundaries_test``), never from these internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from knowledge.api import get_embedding_client, get_store, search_notes

if TYPE_CHECKING:
    # Type-only import so ``Session`` does not leak into the knowledge package
    # namespace (the bdd_completeness_test coverage contract scans public
    # callables). The annotation below is a string under future annotations.
    from sqlmodel import Session

__all__ = [
    "register",
    "register_public",
    "on_startup_jobs",
    "search_notes",
    "get_store",
    "get_embedding_client",
]


def register(app: FastAPI) -> None:
    """Register knowledge domain routers with the app (full private surface)."""
    from knowledge.public_router import router as public_router
    from knowledge.router import router
    from knowledge.tasks_router import router as tasks_router

    app.include_router(router)
    app.include_router(tasks_router)
    # Mount the public routes on the private app too so /api/knowledge/public/*
    # keeps serving exactly as before the public split.
    app.include_router(public_router)


def register_public(app: FastAPI) -> None:
    """Register only the public, read-only knowledge routes."""
    from knowledge.public_router import router as public_router

    app.include_router(public_router)


def on_startup_jobs(session: Session) -> None:
    """Register knowledge scheduled jobs (private binary only).

    The public binary never calls this (app/main_public.py runs no scheduler),
    so the repo-docs reconcile, which writes the knowledge schema, only ever runs
    where there is write access.
    """
    from scheduler.api import register_job
