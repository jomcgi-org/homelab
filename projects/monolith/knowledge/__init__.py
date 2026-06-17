"""Knowledge domain: knowledge graph CRUD, search, tasks, and dead-letter management.

The cross-domain public surface lives in ``knowledge.api``; its callables are
re-exported here so the public-function coverage contract (see
``app/bdd_completeness_test.py``) keeps tracking them under their
``knowledge.*`` names. Other domains must import from ``knowledge.api``
(enforced by ``import_boundaries_test``), never from these internals.
"""

from __future__ import annotations

from fastapi import FastAPI

from knowledge.api import get_embedding_client, get_store, search_notes

__all__ = [
    "register",
    "register_public",
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
