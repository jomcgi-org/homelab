"""Knowledge domain public API: the only surface other domains may import.

Other domains must import from ``knowledge.api`` (enforced by
``import_boundaries_test``), never from ``knowledge`` internals such as
``knowledge.store``. ``knowledge/__init__.py`` re-exports the callables here so
the public-function coverage contract (see ``app/bdd_completeness_test.py``)
keeps tracking them under their ``knowledge.*`` names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from knowledge.gardener import MAX_GARDENER_RETRIES
from knowledge.store import KnowledgeStore  # re-exported for cross-domain typing

if TYPE_CHECKING:
    from sqlmodel import Session

__all__ = [
    "KnowledgeStore",
    "MAX_GARDENER_RETRIES",
    "get_store",
    "search_notes",
    "search_public_chunks",
    "get_embedding_client",
    "ingest_raw",
    "count_notes_review_queue",
    "count_gaps_review_queue",
    "list_tasks_daily",
    "list_tasks_weekly",
]

# Over-fetch factor for the public-chunk search: pull this many times the
# requested note count in chunks so a few notes that contribute multiple nearby
# chunks do not crowd out the rest before we have K distinct notes. The public
# set is small and slow-changing, so a generous over-fetch is cheap.
_PUBLIC_CHUNK_OVERFETCH = 8


def ingest_raw(
    session: "Session",
    *,
    content: str,
    source: str,
    original_url: str | None = None,
    extra: dict | None = None,
):
    """Persist raw content into the knowledge pipeline (knowledge.ingest_queue).

    The ingest stack is imported lazily so cross-domain callers of this facade
    do not pull trafilatura, youtube, and the S3 raw store at load time.
    """
    from knowledge.ingest_queue import ingest_raw as _ingest_raw

    return _ingest_raw(
        session,
        content=content,
        source=source,
        original_url=original_url,
        extra=extra,
    )


def search_notes(session: "Session", query_embedding: list[float], **kwargs):
    """Search knowledge notes by embedding similarity."""
    return KnowledgeStore(session).search_notes_with_context(
        query_embedding=query_embedding, **kwargs
    )


def search_public_chunks(
    session: "Session", query_embedding: list[float], *, limit: int = 6
) -> list[dict]:
    """pgvector cosine search over the public-only chunk view.

    Reads ``public_api.knowledge_chunks`` (chunks of public, non-deleted notes
    only, DB-enforced) and returns up to ``limit`` DISTINCT public notes, each with
    its best-matching chunk, ordered by ascending cosine distance. Confinement is
    the view, not this code: a private note's chunks are physically absent from the
    view, so no query can surface one. Caller passes a public_reader session bound
    to the read replica.

    Returns dicts ``{"note_id", "title", "chunk_text", "score"}`` where
    ``score = 1 - cosine_distance`` (higher is closer).
    """
    from sqlmodel import select

    from knowledge.public_models import PublicChunk

    distance = PublicChunk.embedding.cosine_distance(query_embedding)
    stmt = (
        select(
            PublicChunk.note_id,
            PublicChunk.title,
            PublicChunk.chunk_text,
            distance.label("distance"),
        )
        .order_by(distance.asc())
        .limit(max(1, limit) * _PUBLIC_CHUNK_OVERFETCH)
    )
    rows = session.execute(stmt).all()

    # Dedupe to the best chunk per note, preserving distance order (the first time
    # a note_id is seen is its closest chunk), until we have ``limit`` notes.
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row.note_id in seen:
            continue
        seen.add(row.note_id)
        out.append(
            {
                "note_id": row.note_id,
                "title": row.title,
                "chunk_text": row.chunk_text,
                "score": 1.0 - float(row.distance),
            }
        )
        if len(out) >= limit:
            break
    return out


def get_store(session: "Session") -> KnowledgeStore:
    """Return a KnowledgeStore instance for the given session."""
    return KnowledgeStore(session)


def get_embedding_client():
    """Return an embedding client instance (DI seam for tests)."""
    from shared.embedding import EmbeddingClient

    return EmbeddingClient()


def count_notes_review_queue(session: "Session", *, limit: int = 200) -> int:
    """Count notes pending review (mode='pending'), capped at ``limit`` rows.

    Reuses the exact query behind ``GET /notes/review-queue``, so the count
    matches what a caller would see on that page (up to ``limit``).
    """
    from knowledge.notes import list_notes_for_review

    return len(list_notes_for_review(session, mode="pending", limit=limit))


def count_gaps_review_queue(session: "Session", *, limit: int = 200) -> int:
    """Count gaps pending review (mode='pending'), capped at ``limit`` rows.

    Reuses the exact query behind ``GET /gaps/review-queue``, so the count
    matches what a caller would see on that page (up to ``limit``).
    """
    from knowledge.gaps import list_gaps_for_review

    return len(list_gaps_for_review(session, mode="pending", limit=limit))


def list_tasks_daily(session: "Session") -> list[dict]:
    """Tasks due today or overdue (delegates to KnowledgeStore.list_tasks_daily)."""
    return KnowledgeStore(session).list_tasks_daily()


def list_tasks_weekly(session: "Session") -> list[dict]:
    """Tasks due this week (delegates to KnowledgeStore.list_tasks_weekly)."""
    return KnowledgeStore(session).list_tasks_weekly()
