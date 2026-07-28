"""Public-safe Grimoire retrieval for grimoire chat (ADR 005 layer 5).

This is the one genuinely new build in grimoire_chat: instead of chat_public's
notes-chunk view, it grounds a turn on the public Grimoire (D&D sourcebook)
corpus via a pgvector cosine search over ``grimoire.embedding``.

Embedding-model-space match (CRITICAL). Cosine distance is only meaningful WITHIN
a single embedding model's vector space; scoring a query vector against vectors
from a different model returns garbage. The Grimoire corpus is embedded with
``voyage-4-nano`` (the ``EmbeddingClient`` default, see grimoire.ingest/extract,
which store ``embed_client.model`` on every ``grimoire.embedding`` row). We embed
the live query with the SAME client factory the private Grimoire search uses,
``knowledge.api.get_embedding_client`` (grimoire.search.search_campaign's embed
path), so the query vector is produced by the same model. As belt-and-braces we
ALSO pass that model to ``grimoire.search.knn_embeddings``, which restricts the
cosine search to rows stored under it, so a future second embedding model in the
table can never be scored against a mismatched query vector.

Public-safety (confinement is data, not a prompt rule):

- Chunk hits are always public (the Grimoire corpus is a global, non-campaign
  library).
- Entity hits are filtered to ``is_global`` ONLY: campaign-private entities
  (``is_global = false``, created inside a private game session) are dropped, the
  same posture ``grimoire.public.py`` uses for every public read. A jailbreak
  cannot surface a private entity because it is filtered out in code here, and the
  public_reader role has no grant rows to widen visibility.

Reads go through the DEFAULT app engine (``core.db.get_session``), which in the
public binary is the read-only ``public_reader`` role on the read replica; it holds
SELECT on ``grimoire.embedding/entity/knowledge_chunk`` (and the typed detail
tables) and nothing that would let it read private campaign data. The embedding
call hits the in-cluster ``inference-embeddings`` endpoint via ``EMBEDDING_URL``
(a values literal, no hardcoded service URL); that endpoint is in-cluster and
unmeshed, so the public namespace's off-cluster egress deny does not block it.

The router injects the returned passages/entities as clearly-delimited reference
DATA (not instructions), and the model has no tools, so retrieved content cannot
act.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_
from sqlmodel import Session, select

from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    Book,
    Embedding,
    Entity,
    KnowledgeChunk,
)
from grimoire.search import OVERFETCH_FACTOR, knn_embeddings
from grimoire.visibility import _flatten_detail
from knowledge.api import get_embedding_client

logger = logging.getLogger(__name__)

# How many distinct corpus passages/entities a turn grounds on. Configurable via
# env (the chart supplies it on the public web binary); starting point ~6.
RETRIEVAL_K = int(os.environ.get("GRIMOIRE_CHAT_RETRIEVAL_K", "6"))

# Per-passage character cap so k retrieved passages cannot blow the model context
# (a Grimoire chunk can be a whole page). The DATA block stays bounded; the model
# still gets the most relevant span of each hit. Configurable, generous default.
_MAX_PASSAGE_CHARS = int(os.environ.get("GRIMOIRE_CHAT_MAX_PASSAGE_CHARS", "1200"))

# Hybrid retrieval: how many is_global entities a single lexical name-match may
# anchor per query (kept small so name matches never crowd out the semantic
# passages), and how many extra slots past k the merged set is allowed so the
# lexical anchors sit ALONGSIDE the vector hits rather than evicting them.
_LEXICAL_ENTITY_LIMIT = int(os.environ.get("GRIMOIRE_CHAT_LEXICAL_ENTITY_LIMIT", "3"))
_MERGE_HEADROOM = int(os.environ.get("GRIMOIRE_CHAT_MERGE_HEADROOM", "3"))

# A lexical name match is a strong, exact signal that the corpus contains the
# named thing, so it is scored above any cosine hit (whose 1 - distance tops out
# near 1.0) to mark it as the anchor. Merge order is still explicit (lexical
# first), so this only affects any downstream score display, not ordering.
_LEXICAL_HIT_SCORE = 2.0

# Query tokens that carry no entity-name signal, dropped before the lexical
# name match so "who is Gundren?" searches on "gundren" (not "who"/"is"). A short,
# deliberately conservative list: proper-noun-ish tokens like "gundren" or
# "beholder" are never in here, so they always survive as name candidates.
_NAME_STOPWORDS = frozenset(
    {
        "who",
        "what",
        "whom",
        "whose",
        "which",
        "when",
        "where",
        "why",
        "how",
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "does",
        "did",
        "can",
        "could",
        "would",
        "should",
        "you",
        "your",
        "this",
        "that",
        "these",
        "those",
        "with",
        "from",
        "about",
        "tell",
        "there",
        "here",
        "have",
        "has",
        "had",
        "into",
        "lair",
        "info",
        "please",
        "explain",
        "describe",
    }
)


@dataclass
class RetrievedPassage:
    """One grounded corpus hit: a chunk of sourcebook prose or a corpus entity.

    ``ref_id`` is the chunk id or entity id (the node_touched id); ``title`` is a
    human label (book + section, or entity name + type); ``text`` is the injected
    reference content (chunk prose, or a compact entity statblock/summary);
    ``kind`` is ``"chunk"`` or ``"entity"``. The touched-node set is exactly the
    returned passages.

    ``entity_type`` is the corpus entity_type (npc/creature/spell/...) for an
    entity hit and ``None`` for a chunk; ``book_id``/``chunk_ref`` are the chunk's
    book and in-book ref for a chunk hit and ``None`` for an entity. They let the
    frontend make the GROUNDED IN chip clickable (deep-link a chunk into the
    reader, open an entity by type) without a second lookup.
    """

    ref_id: str
    title: str
    text: str
    kind: str
    score: float
    entity_type: str | None = None
    book_id: str | None = None
    chunk_ref: str | None = None


def _truncate(text: str) -> str:
    text = text or ""
    if len(text) <= _MAX_PASSAGE_CHARS:
        return text
    return text[:_MAX_PASSAGE_CHARS].rstrip() + " ..."


def _resolve_chunk(
    session: Session, chunk_id: str, distance: float
) -> RetrievedPassage | None:
    """Shape a chunk hit: sourcebook prose titled by its book + section."""
    chunk = session.get(KnowledgeChunk, chunk_id)
    if chunk is None:
        return None
    book = session.get(Book, chunk.book_id)
    display = book.display_name if book else chunk.book_id
    title = f"{display}: {chunk.section_path}" if chunk.section_path else display
    return RetrievedPassage(
        ref_id=chunk.id,
        title=title,
        text=_truncate(chunk.content),
        kind="chunk",
        score=1.0 - distance,
        book_id=chunk.book_id,
        chunk_ref=chunk.chunk_ref,
    )


def _entity_statblock(entity: Entity, detail: Any | None) -> str:
    """A compact one-block summary of an entity from its typed detail + spine.

    Folds the typed detail table columns (creature/spell/location/npc) and the
    generic ``detail`` JSONB (gameplay/mechanics types) into ``key: value`` lines
    under a ``Name (entity_type)`` header. Empty/None values are dropped so the
    block stays tight. This is display-only reference material; no private field
    is reachable (the source entity is already is_global-gated by the caller).
    """
    header = f"{entity.name} ({entity.entity_type})"
    fields: dict[str, Any] = {}
    fields.update(_flatten_detail(detail))
    # The generic typed-detail payload for the types with no dedicated table.
    if isinstance(entity.detail, dict):
        fields.update(entity.detail)
    parts = []
    for key, value in fields.items():
        if value in (None, "", {}, []):
            continue
        parts.append(f"{key}: {value}")
    body = "; ".join(parts)
    return f"{header}\n{body}" if body else header


def _resolve_entity(
    session: Session, entity_id: str, distance: float
) -> RetrievedPassage | None:
    """Shape an entity hit, dropping any non-is_global (campaign-private) entity.

    is_global is the public-safety boundary: a campaign-private entity created in
    a session is never surfaced to the anonymous public tier, matching
    grimoire.public.get_entity_public.
    """
    entity = session.get(Entity, entity_id)
    if entity is None or not entity.is_global:
        return None
    detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
    detail = session.get(detail_model, entity_id) if detail_model else None
    return RetrievedPassage(
        ref_id=entity.id,
        title=f"{entity.name} ({entity.entity_type})",
        text=_truncate(_entity_statblock(entity, detail)),
        kind="entity",
        score=1.0 - distance,
        entity_type=entity.entity_type,
    )


def _resolve_hits(
    session: Session,
    hits: list[tuple[Embedding, float]],
    limit: int,
) -> list[RetrievedPassage]:
    """Resolve raw kNN hits into scored passages, deduped by ref_id, up to limit.

    Private entity hits (non-is_global) and dangling ids resolve to None and are
    dropped, so over-fetching keeps the final set from being starved by them.
    """
    out: list[RetrievedPassage] = []
    seen: set[str] = set()
    for embedding, distance in hits:
        if embedding.embeddable_kind == "chunk":
            resolved = _resolve_chunk(session, embedding.embeddable_id, distance)
        elif embedding.embeddable_kind == "entity":
            resolved = _resolve_entity(session, embedding.embeddable_id, distance)
        else:
            resolved = None
        if resolved is None or resolved.ref_id in seen:
            continue
        seen.add(resolved.ref_id)
        out.append(resolved)
        if len(out) >= limit:
            break
    return out


def _candidate_name_tokens(query: str) -> list[str]:
    """Entity-name candidate tokens from a query: alphanumeric runs of length
    >= 3 that are not question/filler stopwords, de-duplicated in order.

    Splitting on non-alphanumerics and dropping the small ``_NAME_STOPWORDS`` set
    leaves the proper-noun-ish tokens ("gundren", "beholder") that a lexical name
    match should anchor on, so "who is Gundren?" reduces to ["gundren"].
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in re.split(r"[^0-9a-zA-Z]+", query.lower()):
        if len(raw) < 3 or raw in _NAME_STOPWORDS or raw in seen:
            continue
        seen.add(raw)
        tokens.append(raw)
    return tokens


def _lexical_entity_hits(
    session: Session, query: str, limit: int
) -> list[RetrievedPassage]:
    """is_global entities whose name contains a candidate query token.

    A pure-vector search can rank a named entity below its more-typical siblings
    (the real "who is Gundren?" miss), so this reuses grimoire.public.search_public's
    case-insensitive name-substring approach to reliably surface a named entity in
    the corpus. Public-safety is unchanged: the same ``Entity.is_global`` gate is
    applied here, so a campaign-private entity can never be anchored, and each hit
    is shaped through the same ``_resolve_entity`` (is_global + statblock) the
    vector path uses. Shortest names first so the tightest match wins the small cap.
    """
    tokens = _candidate_name_tokens(query)
    if not tokens:
        return []
    rows = session.exec(
        select(Entity.id)
        .where(
            or_(*(func.lower(Entity.name).contains(tok) for tok in tokens)),
            Entity.is_global,
        )
        .order_by(func.length(Entity.name), Entity.name, Entity.id)
        .limit(limit)
    ).all()
    hits: list[RetrievedPassage] = []
    for entity_id in rows:
        resolved = _resolve_entity(session, entity_id, 0.0)
        if resolved is None:
            continue
        # Mark as the exact-match anchor; merge order is still explicit below.
        resolved.score = _LEXICAL_HIT_SCORE
        hits.append(resolved)
    return hits


def _merge_hits(
    lexical: list[RetrievedPassage],
    vector: list[RetrievedPassage],
    cap: int,
) -> list[RetrievedPassage]:
    """Merge lexical anchors FIRST, then vector hits, dedup by ref_id, up to cap.

    Lexical name matches lead so a named entity reliably surfaces; the vector hits
    follow so semantic passages are still present. ``cap`` is k + a small headroom
    so a couple of anchors do not fully evict the semantic set.
    """
    merged: list[RetrievedPassage] = []
    seen: set[str] = set()
    for passage in (*lexical, *vector):
        if passage.ref_id in seen:
            continue
        seen.add(passage.ref_id)
        merged.append(passage)
        if len(merged) >= cap:
            break
    return merged


async def retrieve(
    session: Session,
    query: str,
    *,
    k: int | None = None,
    embed_client: Any | None = None,
) -> list[RetrievedPassage]:
    """Embed ``query`` and return up to ``k`` distinct public corpus passages.

    Best-effort: an empty/blank query, no matches, or a transient embedder failure
    all yield an empty list so the turn proceeds ungrounded rather than erroring.
    Failing open is safe because confinement is the data (is_global + the corpus
    tables the public_reader role can read), not retrieval: the worst case is a
    less-grounded answer, never a leaked private entity.
    """
    if not query or not query.strip():
        return []
    limit = k if k is not None else RETRIEVAL_K
    client = embed_client or get_embedding_client()
    # No embeddings endpoint configured: ground nothing rather than attempt a
    # network call (keeps unconfigured/dev/test turns fast and ungrounded).
    if embed_client is None and not getattr(client, "base_url", None):
        return []
    # The stored corpus embeddings live in this model's vector space; pass it to
    # knn_embeddings so the cosine search never mixes model spaces (see docstring).
    model = getattr(client, "model", None)
    try:
        vector = await client.embed(query)
        hits = knn_embeddings(
            session,
            vector,
            ("chunk", "entity"),
            max(1, limit) * OVERFETCH_FACTOR,
            model=model,
        )
        vector_passages = _resolve_hits(session, hits, limit)
        # Hybrid: a lexical entity-name match reliably surfaces a named entity the
        # pure-vector search ranked below its siblings. Anchors lead, vector hits
        # follow, deduped and capped at k + a small headroom.
        lexical_passages = _lexical_entity_hits(session, query, _LEXICAL_ENTITY_LIMIT)
        passages = _merge_hits(
            lexical_passages, vector_passages, limit + _MERGE_HEADROOM
        )
    except Exception:  # noqa: BLE001 - retrieval is best-effort; never fail a turn
        logger.exception("grimoire_chat.retrieval.failed; answering ungrounded")
        return []
    return passages
