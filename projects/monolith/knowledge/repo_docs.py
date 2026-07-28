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


def apply_deletions(session, paths: list[str]) -> int:
    """Delete vanished docs (and their chunks) in one committed transaction.

    Kept separate from the per-doc upserts so deletions are durable immediately,
    independent of the upsert loop. Returns the number of docs deleted.
    """
    from knowledge.models import RepoDoc, RepoDocChunk

    deleted = 0
    for path in paths:
        doc = session.query(RepoDoc).filter_by(path=path).first()
        if doc is None:
            continue
        session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).delete()
        session.delete(doc)
        deleted += 1
    session.commit()
    return deleted


def upsert_doc(
    session, entry: ManifestEntry, chunks: list[Chunk], vectors: list[list[float]]
) -> None:
    """Insert-or-replace a single doc and its chunk set, committed on its own.

    Per-doc commit is what makes the reconcile resumable: a run interrupted by a
    pod rollout mid-backfill leaves every already-committed doc in place, and the
    next run's hash diff (``plan_reconcile``) skips those and continues from the
    remainder, instead of re-embedding all docs from scratch. ``vectors`` must
    line up 1:1 with ``chunks`` (the caller guarantees this).
    """
    from knowledge.models import RepoDoc, RepoDocChunk

    doc = session.query(RepoDoc).filter_by(path=entry.path).first()
    if doc is None:
        doc = RepoDoc(
            path=entry.path, content_hash=entry.sha256, title=_title_for(entry)
        )
        session.add(doc)
    else:
        doc.content_hash = entry.sha256
        doc.title = _title_for(entry)
        session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).delete()
    session.flush()  # assign doc.id for a new doc before building chunk FKs

    rows = [
        RepoDocChunk(
            repo_doc_fk=doc.id,
            chunk_index=c["index"],
            section_header=c["section_header"],
            chunk_text=c["text"],
            embedding=vectors[i],
        )
        for i, c in enumerate(chunks)
    ]
    session.add_all(rows)
    session.commit()


def _plan_in_thread(entries: list[ManifestEntry]) -> ReconcilePlan:
    from sqlmodel import Session

    from core.db import get_engine

    with Session(get_engine()) as session:
        return plan_reconcile(session, entries)


def _delete_in_thread(paths: list[str]) -> int:
    from sqlmodel import Session

    from core.db import get_engine

    with Session(get_engine()) as session:
        return apply_deletions(session, paths)


def _upsert_doc_in_thread(entry: ManifestEntry, chunks, vectors) -> None:
    from sqlmodel import Session

    from core.db import get_engine

    with Session(get_engine()) as session:
        upsert_doc(session, entry, chunks, vectors)


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

    # Deletions first, in one committed transaction.
    deleted = 0
    if plan.to_delete:
        deleted = await asyncio.to_thread(_delete_in_thread, plan.to_delete)

    # Then embed and commit each doc on its own: embedding (network) is awaited
    # here, and the single-doc DB write goes through its own threaded session.
    # Committing per doc keeps a large backfill resumable across a pod rollout.
    client = EmbeddingClient()
    upserted = 0
    failed = 0
    for entry, chunks in plan.to_upsert:
        texts = [c["text"] for c in chunks]
        try:
            vectors = await client.embed_batch(texts)
        except Exception:  # noqa: BLE001 - skip this doc; next run retries it
            logger.exception("repo_docs: embedding failed for %s; skipping", entry.path)
            failed += 1
            continue
        # A short return would zero-pad trailing chunks (a zero vector poisons
        # cosine retrieval), so skip the doc on a count mismatch; its hash stays
        # unchanged and the next run retries it.
        if len(vectors) != len(texts):
            logger.error(
                "repo_docs: embedder returned %d vectors for %d chunks of %s; skipping",
                len(vectors),
                len(texts),
                entry.path,
            )
            failed += 1
            continue
        try:
            await asyncio.to_thread(_upsert_doc_in_thread, entry, chunks, vectors)
        except Exception:  # noqa: BLE001 - isolate a bad doc; never abort the run
            # One doc's DB error (e.g. a stray byte Postgres rejects) must not
            # sink the whole backfill. Skip it; its hash stays unchanged so the
            # next run retries, and the other docs still commit per-doc.
            logger.exception("repo_docs: upsert failed for %s; skipping", entry.path)
            failed += 1
            continue
        upserted += 1

    logger.info(
        "repo_docs: reconciled upserted=%d deleted=%d failed=%d",
        upserted,
        deleted,
        failed,
    )
    return None
