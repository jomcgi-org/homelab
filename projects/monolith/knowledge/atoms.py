"""Shared fileless knowledge atom indexing."""

from __future__ import annotations

import yaml
from sqlmodel import Session

from knowledge.gardener import _slugify
from knowledge.indexing import index_note_from_raw
from knowledge.store import KnowledgeStore
from shared.embedding import EmbeddingClient


async def index_atom(
    session: Session,
    *,
    title: str,
    body: str,
    type: str,
    visibility: str,
    source_tier: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    edges: dict[str, list[str]] | None = None,
    derived_from_raw: str | None = None,
    status: str | None = None,
    size: str | None = None,
    due: str | None = None,
    blocked_by: list[str] | None = None,
    scope: str | None = None,
    verification_state: str | None = None,
    confidence: float | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    observed_at: str | None = None,
    commit: bool = True,
    _store_factory=None,
    _embedding_client_factory=None,
    _indexer=None,
    _vectors: list[list[float]] | None = None,
) -> str:
    """Build and index a fileless atom, returning its stable note id."""
    store_factory = _store_factory or KnowledgeStore
    embedding_client_factory = _embedding_client_factory or EmbeddingClient
    indexer = _indexer or index_note_from_raw
    store = store_factory(session)
    base = _slugify(title)
    note_id = base
    counter = 1
    while store.get_note_by_id(note_id) is not None:
        note_id = f"{base}-{counter}"
        counter += 1

    fm_dict: dict[str, object] = {
        "id": note_id,
        "title": title,
        "type": type,
        "visibility": visibility,
    }
    optional = {
        "source_tier": source_tier,
        "derived_from_raw": derived_from_raw,
        "scope": scope,
        "verification_state": verification_state,
        "confidence": confidence,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "observed_at": observed_at,
    }
    for key, value in optional.items():
        if value is not None:
            fm_dict[key] = value
    if tags:
        fm_dict["tags"] = list(tags)
    if aliases:
        fm_dict["aliases"] = list(aliases)
    if edges:
        fm_dict["edges"] = {key: list(value) for key, value in edges.items()}
    if type == "active":
        fm_dict["status"] = status
        fm_dict["size"] = size
        if due is not None:
            fm_dict["due"] = due
        if blocked_by:
            fm_dict["blocked_by"] = list(blocked_by)

    fm_str = yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)
    raw = f"---\n{fm_str}---\n\n{body.strip()}\n"
    index_kwargs = {
        "note_id": note_id,
        "rel_path": f"_processed/{note_id}.md",
        "raw": raw,
        "commit": commit,
    }
    if _vectors is not None:
        index_kwargs["vectors"] = _vectors
    await indexer(store, embedding_client_factory(), **index_kwargs)
    return note_id
