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

__all__ = ["register", "search_notes", "get_store", "get_embedding_client"]


def register(app: FastAPI) -> None:
    """Register knowledge domain routers with the app."""
    from knowledge.router import router
    from knowledge.tasks_router import router as tasks_router

    app.include_router(router)
    app.include_router(tasks_router)
