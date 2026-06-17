"""Knowledge domain public API: the only surface other domains may import.

Other domains must import from ``knowledge.api`` (enforced by
``import_boundaries_test``), never from ``knowledge`` internals such as
``knowledge.store``. ``knowledge/__init__.py`` re-exports the callables here so
the public-function coverage contract (see ``app/bdd_completeness_test.py``)
keeps tracking them under their ``knowledge.*`` names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from knowledge.store import KnowledgeStore  # re-exported for cross-domain typing

if TYPE_CHECKING:
    from sqlmodel import Session

__all__ = ["KnowledgeStore", "get_store", "search_notes", "get_embedding_client"]


def search_notes(session: "Session", query_embedding: list[float], **kwargs):
    """Search knowledge notes by embedding similarity."""
    return KnowledgeStore(session).search_notes_with_context(
        query_embedding=query_embedding, **kwargs
    )


def get_store(session: "Session") -> KnowledgeStore:
    """Return a KnowledgeStore instance for the given session."""
    return KnowledgeStore(session)


def get_embedding_client():
    """Return an embedding client instance (DI seam for tests)."""
    from shared.embedding import EmbeddingClient

    return EmbeddingClient()
