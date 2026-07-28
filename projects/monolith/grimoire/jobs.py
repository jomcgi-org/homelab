"""Scheduled job handlers for the grimoire ingest pipeline (spec #4.2).

Two daily, idempotent batch jobs wrap the async orchestrators in ``ingest.py``
and ``extract.py``:

  - ``grimoire_load_chunks``: build an S3 client + embedding client, then run
    ``ingest.load_chunks`` over the ``grimoire`` bucket.
  - ``grimoire_extract_entities``: build an OpenRouter client (skipping the run
    with a warning when OPENROUTER_API_KEY is unset, never crashing the
    scheduler) + embedding client, then run ``extract.extract_chunks``.

Both run off-pod as Argo CronWorkflows: ``app/jobs_main.py`` exposes the
``grimoire-load-chunks`` and ``grimoire-extract-entities`` subcommands (via the
shared ``_run_job`` helper), and the ``jobs.cronWorkflows`` registry in
chart/values.yaml schedules them (loader daily; extraction suspended /
manual-only, since it costs OpenRouter money). Each is an ``async def`` keeping
the scheduler Handler contract (``scheduler.api.Handler``: receives a Session,
returns an optional next-run override), so ``_run_job`` opens a Session and
awaits it directly. This module is excluded from the public binary (grimoire is
private-tier only).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os

from sqlmodel import Session, select

from grimoire.models import KnowledgeChunk

logger = logging.getLogger("monolith.grimoire.jobs")

DEFAULT_BUCKET = "grimoire"
# Books live under ``books/<book_id>/`` in the grimoire bucket; the verbatim
# Marker extraction is archived under ``raw/`` (see grimoire/tools/upload-book.sh)
# as a gzipped ``output.json``. Mirrors ingest.DEFAULT_PREFIX.
_BOOKS_PREFIX = "books/"
DEFAULT_EXTRACT_LIMIT = 25
# Concurrent extract calls; kept below vLLM --max-num-seqs (8) so this bulk job
# leaves decode-slot headroom for trusted interactive callers. See
# grimoire.extract.DEFAULT_CONCURRENCY.
DEFAULT_EXTRACT_CONCURRENCY = 6


def _embedding_client():
    """Build the shared embedding client, reusing knowledge's DI seam.

    Imported lazily so this module never pulls the embedding/knowledge import
    closure at grimoire import time.
    """
    from knowledge.api import get_embedding_client

    return get_embedding_client()


def _extract_limit() -> int:
    """Read GRIMOIRE_EXTRACT_LIMIT, falling back to the default on a bad value."""
    raw = os.environ.get("GRIMOIRE_EXTRACT_LIMIT", str(DEFAULT_EXTRACT_LIMIT))
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "grimoire_extract_entities: invalid GRIMOIRE_EXTRACT_LIMIT %r, using %d",
            raw,
            DEFAULT_EXTRACT_LIMIT,
        )
        return DEFAULT_EXTRACT_LIMIT


def _extract_concurrency() -> int:
    """Read GRIMOIRE_EXTRACT_CONCURRENCY (concurrent vLLM extract calls).

    Falls back to the default on a bad or non-positive value; keep it <= the
    vLLM server's --max-num-seqs so requests batch rather than queue.
    """
    raw = os.environ.get(
        "GRIMOIRE_EXTRACT_CONCURRENCY", str(DEFAULT_EXTRACT_CONCURRENCY)
    )
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning(
            "grimoire_extract_entities: invalid GRIMOIRE_EXTRACT_CONCURRENCY %r, "
            "using %d",
            raw,
            DEFAULT_EXTRACT_CONCURRENCY,
        )
        return DEFAULT_EXTRACT_CONCURRENCY
    return value


async def grimoire_load_chunks(session: Session) -> None:
    """Load S3 chunk manifests into knowledge_chunk + embedding (spec #4.2.1).

    Idempotent: ``load_chunks`` upserts by ``(book_id, chunk_ref)`` and only
    re-embeds new/changed content, so a re-run over unchanged manifests is
    cheap. Returns None so the scheduler advances by the configured interval.
    """
    from grimoire.ingest import build_s3_client, load_chunks

    bucket = os.environ.get("GRIMOIRE_S3_BUCKET", DEFAULT_BUCKET)
    s3_client = build_s3_client()
    embed_client = _embedding_client()
    summary = await load_chunks(session, s3_client, embed_client, bucket)
    logger.info("grimoire_load_chunks done: %s", summary)
    return None


async def grimoire_extract_entities(session: Session) -> None:
    """Extract entities/mentions/relationships from pending chunks (spec #4.2.2).

    Gates on the configured endpoint, not key presence: a hosted endpoint (the
    default / an openrouter.ai URL, or the direct DeepSeek API) needs a key, so
    the run skips (logs a warning, does not raise) when the key is unset, so a
    missing secret degrades this one job rather than crashing the scheduler tick.
    The key is read provider-agnostically from GRIMOIRE_EXTRACT_API_KEY, falling
    back to OPENROUTER_API_KEY. Any other base_url (e.g. the in-cluster Qwen vLLM
    endpoint) runs keyless. Bounded per run by GRIMOIRE_EXTRACT_LIMIT (default
    25) so a run fits the job deadline. Returns None so the scheduler advances by
    the configured interval.
    """
    from grimoire.extract import OpenRouterClient, extract_chunks

    base_url = os.environ.get("GRIMOIRE_EXTRACT_BASE_URL", "")
    api_key = os.environ.get("GRIMOIRE_EXTRACT_API_KEY") or os.environ.get(
        "OPENROUTER_API_KEY", ""
    )
    # Hosted (keyed) endpoints: OpenRouter (default / openrouter.ai) or direct
    # DeepSeek. Anything else (in-cluster vLLM) runs keyless.
    needs_key = (
        (not base_url)
        or "openrouter.ai" in base_url
        or ("api.deepseek.com" in base_url)
    )
    if needs_key and not api_key:
        logger.warning(
            "grimoire_extract_entities: hosted endpoint but no key "
            "(GRIMOIRE_EXTRACT_API_KEY / OPENROUTER_API_KEY unset), skipping run"
        )
        return None

    limit = _extract_limit()
    concurrency = _extract_concurrency()
    or_client = OpenRouterClient(api_key=api_key)  # base_url/model read from env
    embed_client = _embedding_client()
    summary = await extract_chunks(
        session, or_client, embed_client, limit=limit, concurrency=concurrency
    )
    logger.info("grimoire_extract_entities done: %s", summary)
    return None


# --- section_hierarchy backfill (spec: metadata-only, chunk_ref-keyed) --------
#
# ``section_hierarchy`` is extraction-context metadata that marker.py only
# started emitting into the chunks NDJSON after the 33 existing books were
# uploaded, so their loaded rows have it NULL. This backfill recomputes the
# ``{chunk_ref: section_hierarchy}`` map by re-running marker's EXISTING chunking
# (grimoire.marker.to_chunks) over each book's archived raw ``output.json`` and
# writes ONLY the section_hierarchy column onto the already-loaded chunks, keyed
# on (book_id, chunk_ref). It NEVER inserts a chunk, never touches
# content/embedding/mentions/relationships/extraction markers, and never
# re-embeds: a chunk whose chunk_ref no longer matches (a since-fixed section
# boundary shifted it) is simply left untouched. Re-running marker+reload would
# be unsafe precisely because a shifted chunk_ref would CREATE a new chunk and
# re-embed; keying the UPDATE on chunk_ref makes the backfill a cheap metadata
# write instead.


def _find_raw_output_key(s3_client, bucket: str, book_id: str) -> str | None:
    """Locate a book's archived Marker ``output.json`` under ``books/<id>/raw/``.

    The uploader gzips it to ``output.json.gz`` (grimoire/tools/upload-book.sh),
    but tolerate an uncompressed ``output.json`` too; prefer the gzipped one when
    both exist. Lists with the same paginated ``list_objects_v2`` pattern
    ingest.py uses. Returns the S3 key or ``None`` if no raw extraction is found.
    """
    prefix = f"{_BOOKS_PREFIX}{book_id}/raw/"
    gz_key: str | None = None
    plain_key: str | None = None
    continuation_token: str | None = None
    while True:
        kwargs: dict[str, str] = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            base = key.rsplit("/", 1)[-1]
            if base == "output.json.gz":
                gz_key = key
            elif base == "output.json":
                plain_key = key
        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break
    return gz_key or plain_key


def _read_raw_doc(s3_client, bucket: str, book_id: str) -> dict | None:
    """Read + parse a book's raw Marker ``output.json`` (gunzipping if needed).

    Returns the JSONOutput dict, or ``None`` when the book has no archived raw
    extraction (older/manually-loaded books) so the caller can skip it.
    """
    key = _find_raw_output_key(s3_client, bucket, book_id)
    if not key:
        return None
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    if key.endswith(".gz"):
        body = gzip.decompress(body)
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def _hierarchy_map_from_doc(
    doc: dict, bucket: str, book_id: str
) -> dict[str, str | None]:
    """Build ``{chunk_ref: section_hierarchy}`` by re-running marker's chunking.

    Uses the SAME entrypoint the loader's manifests were produced with
    (grimoire.marker.to_chunks with the identical image_key_prefix), so chunk_ref
    is computed identically and matches the already-loaded rows. Chunking output
    is otherwise discarded: only chunk_ref + section_hierarchy are read.
    """
    from grimoire import marker

    image_key_prefix = f"s3://{bucket}/{_BOOKS_PREFIX}{book_id}/raw/img/"
    chunks = marker.to_chunks(doc, image_key_prefix=image_key_prefix)
    mapping: dict[str, str | None] = {}
    for chunk in chunks:
        chunk_ref = chunk.get("chunk_ref")
        if chunk_ref:
            mapping[chunk_ref] = chunk.get("section_hierarchy")
    return mapping


def _apply_hierarchy_updates(
    session: Session, book_id: str, hierarchy_by_ref: dict[str, str | None]
) -> tuple[int, int]:
    """Sync core: write ONLY section_hierarchy onto existing (book_id, chunk_ref)
    rows. Returns ``(chunk_ref_matched, updated)``; does not commit (the caller
    owns the per-book commit so tests can drive it directly).

    Selects the already-loaded chunks whose chunk_ref is in the map and mutates
    section_hierarchy in place only when it actually differs (``IS DISTINCT
    FROM`` semantics, NULL-safe in Python). Rows are already session-tracked from
    the SELECT, so the mutation flushes on commit with no ``session.add`` and no
    per-row ``session.execute`` (semgrep session-add-in-loop). No row is ever
    inserted: a chunk_ref present in the map but absent from the table (a shifted
    boundary) simply does not appear in ``rows`` and is skipped. No other column,
    embedding, or extraction marker is touched.
    """
    if not hierarchy_by_ref:
        return 0, 0
    rows = list(
        session.execute(
            select(KnowledgeChunk).where(
                KnowledgeChunk.book_id == book_id,
                KnowledgeChunk.chunk_ref.in_(list(hierarchy_by_ref)),
            )
        )
        .scalars()
        .all()
    )
    updated = 0
    for row in rows:
        new_hierarchy = hierarchy_by_ref[row.chunk_ref]
        if row.section_hierarchy != new_hierarchy:
            row.section_hierarchy = new_hierarchy
            updated += 1
    return len(rows), updated


def _persist_book_hierarchy(
    book_id: str, hierarchy_by_ref: dict[str, str | None]
) -> tuple[int, int]:
    """Open a fresh session, apply one book's hierarchy updates, and commit.

    Runs off the event loop via ``asyncio.to_thread`` (so it owns its own
    session, never the scheduler's), committing per book for incremental
    durability. Delegates the testable write to ``_apply_hierarchy_updates``.
    """
    from core.db import get_engine

    with Session(get_engine()) as session:
        matched, updated = _apply_hierarchy_updates(session, book_id, hierarchy_by_ref)
        session.commit()
    return matched, updated


def _distinct_book_ids(only_book: str | None) -> list[str]:
    """Book ids to backfill: the single scoped book, or every book with chunks.

    ``GRIMOIRE_BACKFILL_BOOK`` (via ``only_book``) scopes to one book for a safe
    single-book verify run. Otherwise reads the distinct book_ids straight off
    knowledge_chunk in a fresh session so only books that actually have loaded
    chunks are processed.
    """
    if only_book:
        return [only_book]
    from core.db import get_engine

    with Session(get_engine()) as session:
        book_ids = (
            session.execute(select(KnowledgeChunk.book_id).distinct()).scalars().all()
        )
    return sorted(book_ids)


async def grimoire_backfill_hierarchy(session: Session) -> None:
    """Backfill section_hierarchy onto already-loaded chunks (metadata-only).

    For each book (all books with chunks, or the single ``GRIMOIRE_BACKFILL_BOOK``
    for a scoped verify): read its archived raw Marker ``output.json`` from S3,
    re-run marker's chunking to recompute ``{chunk_ref: section_hierarchy}``, and
    UPDATE only the section_hierarchy column of matching (book_id, chunk_ref)
    rows. Never inserts a chunk, never re-embeds, never touches any other column
    or the extraction markers. S3 reads happen here in the async handler; every
    Session write is delegated to ``asyncio.to_thread`` with a fresh session
    (monolith async-handler rule), committing per book. Returns None so the
    scheduler advances by the configured interval.
    """
    from grimoire.ingest import build_s3_client

    bucket = os.environ.get("GRIMOIRE_S3_BUCKET", DEFAULT_BUCKET)
    only_book = os.environ.get("GRIMOIRE_BACKFILL_BOOK") or None
    s3_client = build_s3_client()

    book_ids = await asyncio.to_thread(_distinct_book_ids, only_book)
    logger.info(
        "grimoire_backfill_hierarchy: %d book(s) to process%s",
        len(book_ids),
        f" (scoped to {only_book!r})" if only_book else "",
    )

    total_chunks_in_raw = 0
    total_matched = 0
    total_updated = 0
    books_processed = 0
    books_skipped = 0

    for book_id in book_ids:
        doc = _read_raw_doc(s3_client, bucket, book_id)
        if doc is None:
            logger.warning(
                "grimoire_backfill_hierarchy: no raw output.json for %s, skipping",
                book_id,
            )
            books_skipped += 1
            continue

        hierarchy_by_ref = _hierarchy_map_from_doc(doc, bucket, book_id)
        chunks_in_raw = len(hierarchy_by_ref)
        matched, updated = await asyncio.to_thread(
            _persist_book_hierarchy, book_id, hierarchy_by_ref
        )
        logger.info(
            "grimoire_backfill_hierarchy: %s chunks_in_raw=%d chunk_ref_matched=%d "
            "updated=%d",
            book_id,
            chunks_in_raw,
            matched,
            updated,
        )
        total_chunks_in_raw += chunks_in_raw
        total_matched += matched
        total_updated += updated
        books_processed += 1

    summary = {
        "books_processed": books_processed,
        "books_skipped": books_skipped,
        "chunks_in_raw": total_chunks_in_raw,
        "chunk_ref_matched": total_matched,
        "updated": total_updated,
    }
    logger.info("grimoire_backfill_hierarchy done: %s", summary)
    return None
