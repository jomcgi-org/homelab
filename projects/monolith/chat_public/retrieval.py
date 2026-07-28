"""Public-graph retrieval for public chat (ADR 005 layer 5, plan Phase 4a).

Confinement is a DATABASE property, never a prompt rule. Retrieval embeds the live
user query and runs a pgvector cosine search over ``public_api.knowledge_chunks``,
a view that exposes only the chunk embeddings of public, non-deleted notes. The
search runs on the DEFAULT app engine (``core.db.get_session``), which in the public
binary is the read-only ``public_reader`` role on the read replica, so a private
note is physically unreadable here regardless of any jailbreak. The matching note
text is returned for grounding; the router injects it as clearly-delimited
reference DATA (not instructions) and the model has no tools, so retrieved content
cannot act.

Reads stay on the public_reader/replica engine (this module is handed that
session); session/transcript WRITES live on the separate public_writer engine
(``chat_public.db``). Embeddings hit the in-cluster ``inference-embeddings``
endpoint via ``EMBEDDING_URL`` (a values literal, no hardcoded service URL in
code); that endpoint is in-cluster and unmeshed, so the public namespace's
off-cluster egress deny does not block it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlmodel import Session

from knowledge.api import search_public_chunks
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)

# How many distinct public notes a turn grounds on. Configurable via env (the
# chart supplies it on the public web binary); the plan's starting point is ~6.
RETRIEVAL_K = int(os.environ.get("CHAT_PUBLIC_RETRIEVAL_K", "6"))


@dataclass
class RetrievedNote:
    """One grounded public note: the touched-node set is exactly these notes."""

    note_id: str
    title: str
    chunk_text: str
    score: float


async def retrieve(
    session: Session,
    query: str,
    *,
    k: int | None = None,
    embed_client: EmbeddingClient | None = None,
) -> list[RetrievedNote]:
    """Embed ``query`` and return up to ``k`` distinct public notes for grounding.

    Best-effort: an empty/blank query, no public matches, or a transient embedder
    failure all yield an empty list so the turn proceeds ungrounded rather than
    erroring. Failing open is safe because confinement is the view, not retrieval:
    the worst case is a less-grounded answer, never a leaked private note.
    """
    if not query or not query.strip():
        return []
    limit = k if k is not None else RETRIEVAL_K
    client = embed_client or EmbeddingClient()
    # No embeddings endpoint configured: ground nothing rather than attempt a
    # network call (keeps unconfigured/dev/test turns fast and ungrounded).
    if embed_client is None and not getattr(client, "base_url", None):
        return []
    try:
        vector = await client.embed(query)
        rows = search_public_chunks(session, vector, limit=limit)
    except Exception:  # noqa: BLE001 - retrieval is best-effort; never fail a turn
        logger.exception("chat_public.retrieval.failed; answering ungrounded")
        return []
    return [
        RetrievedNote(
            note_id=row["note_id"],
            title=row["title"],
            chunk_text=row["chunk_text"],
            score=row["score"],
        )
        for row in rows
    ]
