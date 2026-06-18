"""Repo-docs ingest: reconcile the baked markdown manifest into isolated
knowledge.repo_docs / repo_doc_chunks tables for public-chat grounding.

Confinement, isolation, and the async/sync split are documented inline at the
relevant functions. This module is imported only by the private binary's
scheduler wiring; the public binary never runs the reconcile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from knowledge.chunker import Chunk, chunk_markdown

logger = logging.getLogger(__name__)

# The manifest sits beside this module in the image runfiles (the :main binary's
# data). An env override exists purely for tests / ops.
_MANIFEST_NAME = "repo_docs_manifest.ndjson"


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    title: str
    content: str


def manifest_path() -> Path:
    override = os.environ.get("REPO_DOCS_MANIFEST_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / _MANIFEST_NAME


def load_manifest(path: Path | None = None) -> list[ManifestEntry]:
    p = path or manifest_path()
    if not p.exists():
        logger.warning("repo_docs: manifest not found at %s; nothing to index", p)
        return []
    entries: list[ManifestEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        entries.append(
            ManifestEntry(
                path=o["path"],
                sha256=o["sha256"],
                title=o["title"],
                content=o["content"],
            )
        )
    return entries


@dataclass
class ReconcilePlan:
    to_upsert: list[tuple[ManifestEntry, list[Chunk]]]
    to_delete: list[str]


@dataclass
class ReconcileStats:
    upserted: int
    deleted: int
    unchanged: int


def _title_for(entry: ManifestEntry) -> str:
    # The generator already derived the title; trust it (fallback already applied).
    return entry.title


def plan_reconcile(session, entries: list[ManifestEntry]) -> ReconcilePlan:
    """Pure diff: compare manifest hashes to the stored hashes and chunk the docs
    that need (re)indexing. No embedding (network) and no writes happen here.
    """
    from knowledge.models import RepoDoc

    existing: dict[str, str] = {
        path: h for path, h in session.query(RepoDoc.path, RepoDoc.content_hash).all()
    }
    manifest_paths = {e.path for e in entries}

    to_upsert: list[tuple[ManifestEntry, list[Chunk]]] = []
    for e in entries:
        if existing.get(e.path) == e.sha256:
            continue  # unchanged
        chunks = chunk_markdown(e.content)
        if not chunks:
            chunks = [{"index": 0, "section_header": "", "text": e.content or e.title}]
        to_upsert.append((e, chunks))

    to_delete = sorted(p for p in existing if p not in manifest_paths)
    return ReconcilePlan(to_upsert=to_upsert, to_delete=to_delete)


def apply_reconcile(
    session, plan: ReconcilePlan, vectors_by_path: dict[str, list[list[float]]]
) -> ReconcileStats:
    """Apply the plan in one transaction: delete vanished docs (+ their chunks),
    and upsert changed/new docs replacing their chunk set. ``vectors_by_path``
    holds one embedding per chunk, in chunk order, keyed by doc path.
    """
    from knowledge.models import RepoDoc, RepoDocChunk

    deleted = 0
    for path in plan.to_delete:
        doc = session.query(RepoDoc).filter_by(path=path).first()
        if doc is None:
            continue
        session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).delete()
        session.delete(doc)
        deleted += 1

    # First pass: resolve every doc (insert-or-update) and clear the chunk set
    # of changed docs. New docs are collected and added in one ``add_all`` after
    # the loop rather than ``session.add`` per iteration: this whole reconcile is
    # a single transaction, so the per-iteration savepoint that semgrep
    # session-add-in-loop would otherwise want is the wrong shape here.
    new_docs: list = []
    resolved: list = []  # (entry, doc, chunks) in plan order, doc id assigned below
    for entry, chunks in plan.to_upsert:
        doc = session.query(RepoDoc).filter_by(path=entry.path).first()
        if doc is None:
            doc = RepoDoc(
                path=entry.path, content_hash=entry.sha256, title=_title_for(entry)
            )
            new_docs.append(doc)
        else:
            doc.content_hash = entry.sha256
            doc.title = _title_for(entry)
            session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).delete()
        resolved.append((entry, doc, chunks))
    session.add_all(new_docs)
    session.flush()  # assign ids to the new docs before building chunk FKs

    # Second pass: build the replacement chunk rows for every resolved doc and
    # insert them in one batch (add_all, not add-in-loop).
    chunk_rows: list = []
    for entry, doc, chunks in resolved:
        vectors = vectors_by_path.get(entry.path) or []
        chunk_rows.extend(
            RepoDocChunk(
                repo_doc_fk=doc.id,
                chunk_index=c["index"],
                section_header=c["section_header"],
                chunk_text=c["text"],
                embedding=vectors[i] if i < len(vectors) else [0.0] * 1024,
            )
            for i, c in enumerate(chunks)
        )
    session.add_all(chunk_rows)

    session.commit()
    return ReconcileStats(upserted=len(plan.to_upsert), deleted=deleted, unchanged=0)


def _plan_in_thread(entries: list[ManifestEntry]) -> ReconcilePlan:
    from sqlmodel import Session

    from app.db import get_engine

    with Session(get_engine()) as session:
        return plan_reconcile(session, entries)


def _apply_in_thread(plan: ReconcilePlan, vectors_by_path) -> ReconcileStats:
    from sqlmodel import Session

    from app.db import get_engine

    with Session(get_engine()) as session:
        return apply_reconcile(session, plan, vectors_by_path)


async def repo_docs_reconcile_handler(session) -> datetime | None:
    """Scheduler handler (private binary only). Diff the baked manifest against the
    DB, embed the changed docs' chunks, and apply. The ``session`` arg is the
    scheduler's loop session and is intentionally NOT used for I/O here (semgrep
    no-session-in-to-thread): every DB touch happens in its own threaded session.
    """
    from shared.embedding import EmbeddingClient

    entries = load_manifest()
    if not entries:
        return None

    plan = await asyncio.to_thread(_plan_in_thread, entries)
    if not plan.to_upsert and not plan.to_delete:
        logger.info("repo_docs: nothing to reconcile (manifest unchanged)")
        return None

    client = EmbeddingClient()
    vectors_by_path: dict[str, list[list[float]]] = {}
    for entry, chunks in plan.to_upsert:
        texts = [c["text"] for c in chunks]
        try:
            vectors = await client.embed_batch(texts)
        except Exception:  # noqa: BLE001 - skip this doc; next run retries it
            logger.exception("repo_docs: embedding failed for %s; skipping", entry.path)
            continue
        # A short return would otherwise zero-pad the trailing chunks (a zero
        # vector poisons cosine retrieval), so drop the whole doc on a count
        # mismatch; its hash stays unchanged and the next run retries it.
        if len(vectors) != len(texts):
            logger.error(
                "repo_docs: embedder returned %d vectors for %d chunks of %s; skipping",
                len(vectors),
                len(texts),
                entry.path,
            )
            continue
        vectors_by_path[entry.path] = vectors

    # Drop upserts whose embedding failed/mismatched so we never persist zero
    # vectors; their hash stays unchanged in the DB so the next run retries them.
    plan.to_upsert = [(e, c) for (e, c) in plan.to_upsert if e.path in vectors_by_path]

    stats = await asyncio.to_thread(_apply_in_thread, plan, vectors_by_path)
    logger.info(
        "repo_docs: reconciled upserted=%d deleted=%d", stats.upserted, stats.deleted
    )
    return None
