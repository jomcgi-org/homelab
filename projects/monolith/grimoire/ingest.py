"""S3 chunk loader: NDJSON manifests -> knowledge_chunk + embedding.

Spec #4.1/#4.2 of the pg-first design: each book's
converted chunks land as NDJSON at ``s3://<bucket>/books/<book_id>/chunks/*.ndjson``
(one chunk object per line), colocated with the verbatim third-party extraction
under ``books/<book_id>/raw/``. This module lists those manifests, upserts rows
into ``knowledge_chunk`` keyed on ``(book_id, chunk_ref)``, and embeds new/changed
content into the generic ``embedding`` table (``embeddable_kind="chunk"``).

The S3 client construction mirrors ``artifact.s3`` / ``trips.s3`` /
``stars.grid._s3_client`` (dummy creds, path-style addressing, scheme guard
on the endpoint); ``build_s3_client`` is the reusable constructor, while
``load_chunks`` itself takes an already-built client so tests can pass a
fake and a later scheduler job (Task 7) can pass a real one without this
module needing to know how it is wired.

All Session I/O (upserts) lives in plain sync helper functions, called
directly from ``load_chunks`` rather than written inline in its ``async
def`` body, mirroring ``hikes.jobs``'s ``_persist_walks`` /
``knowledge.store.KnowledgeStore.upsert_note`` pattern: the sync work is
callable on its own, and the caller (a job handler, Task 7) owns any
``asyncio.to_thread`` wrapping needed to keep it off the event loop. Each
upsert runs inside its own savepoint so one bad row does not roll back its
siblings in the same batch.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from typing import Any, Protocol

from sqlmodel import Session, select

from grimoire.models import Book, Embedding, KnowledgeChunk

logger = logging.getLogger("monolith.grimoire.ingest")


def _default_display_name(book_id: str) -> str:
    """Human-friendly default title for a book id slug: "monster-manual" ->
    "Monster Manual".
    """
    return book_id.replace("-", " ").replace("_", " ").title()


# Book slugs whose full text is under an open license that permits public
# redistribution (Creative Commons CC BY 4.0 and/or the ORC License), so the
# public tier may serve their verbatim Reader. Everything else is treated as
# copyrighted and Reader-locked (grimoire.book.copyrighted_content defaults
# TRUE). Keeping this list here — the one place _upsert_book classifies a
# freshly seen book — means a future BFRD / A5E ingest self-classifies as open
# without a manual DB flip. The DB column stays the authoritative gate; this
# only seeds the default at first insert.
OPEN_LICENSE_BOOK_IDS = frozenset(
    {
        "system-reference-doc-5-1",  # D&D SRD 5.1, WotC, CC BY 4.0
        "system-reference-doc-5-2",  # D&D SRD 5.2, WotC, CC BY 4.0
        "black-flag-reference-document",  # Kobold Press BFRD, ORC + CC BY 4.0
        "a5e-srd",  # Level Up: Advanced 5e SRD, EN Publishing, ORC + CC BY 4.0
    }
)


DEFAULT_PREFIX = "books/"
_MANIFEST_SUFFIX = ".ndjson"
# Manifests live under ``<prefix><book_id>/chunks/``; this segment both filters
# chunk manifests from other book files and delimits the book id in the key.
_CHUNKS_SEGMENT = "/chunks/"

# Embeddings are grouped by a content-size budget, not a fixed item count. The
# embedding server's latency is token-bound: 64 small front-matter chunks embed
# in ~4s, but 64 stat-block chunks (~32k tokens) took ~78s and blew the client's
# 60s read timeout, so the loader retried forever and left the book unembedded.
# Capping each batch by summed content chars (~4 chars/token) keeps every request
# well under the timeout regardless of chunk sizes; the item cap stops a flood of
# tiny chunks from building an oversized request array.
EMBED_CHAR_BUDGET = (
    60000  # ~15k tokens, ~36s server-side, safe even under the default 60s read timeout
)
EMBED_MAX_BATCH = 64
# Hard cap on a SINGLE text sent for embedding. The llama.cpp embedding server
# rejects (HTTP 500) any input longer than its 8192-token slot, and dense table
# chunks tokenize at roughly 2.5-3 chars/token, so 12000 chars keeps the worst
# case comfortably inside the window. Applies to the embed INPUT only: the
# stored chunk keeps its full content for retrieval. Seen live 2026-07-04:
# 20k-47k char merged-table chunks (DMG'24 magic items, PHB'24 spell lists)
# failed every grimoire-load-chunks run until capped.
EMBED_INPUT_MAX_CHARS = 12000


class _Embedder(Protocol):
    model: str

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def build_s3_client():
    """Build a SeaweedFS S3 client (mirrors artifact.s3._client / trips.s3).

    boto3 is imported lazily so importing this module never pulls boto3 into
    an import closure that does not need it.
    """
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT unset; cannot load grimoire chunks")
    # The chart injects a scheme-less host:port; boto3 requires a scheme and
    # SeaweedFS S3 is plaintext HTTP.
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    # Scheme guaranteed above; see artifact.s3._client for the same guard.
    return boto3.client(  # nosemgrep: boto3-endpoint-url-missing-scheme
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "duckdb"),
        aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "duckdb"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def parse_manifest_lines(book_id: str, lines: Iterable[str]) -> tuple[list[dict], int]:
    """Parse NDJSON manifest lines for one book (spec #4.1 shape).

    Required: ``chunk_ref`` (non-empty str), ``content`` (non-empty str).
    Optional: ``section_path`` (str, else dropped); ``section_hierarchy`` (str
    full ancestor breadcrumb for extraction context, else dropped); ``image_ref``
    (str s3:// URI for image-derived chunks, else dropped); ``meta`` and any other
    field is ignored. A line that is invalid JSON, not an object, or missing a
    valid required field is counted as an error and logged at warning (never
    raised) so one bad line never fails the batch.

    Returns ``(valid_chunks, error_count)`` where each valid chunk is
    ``{"chunk_ref", "content", "section_path", "section_hierarchy", "image_ref"}``.
    """
    valid: list[dict] = []
    errors = 0
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "grimoire ingest: invalid JSON in %s line %d", book_id, lineno
            )
            errors += 1
            continue
        if not isinstance(obj, dict):
            logger.warning(
                "grimoire ingest: non-object line in %s line %d", book_id, lineno
            )
            errors += 1
            continue

        chunk_ref = obj.get("chunk_ref")
        content = obj.get("content")
        if not isinstance(chunk_ref, str) or not chunk_ref:
            logger.warning(
                "grimoire ingest: missing/invalid chunk_ref in %s line %d",
                book_id,
                lineno,
            )
            errors += 1
            continue
        if not isinstance(content, str) or not content:
            logger.warning(
                "grimoire ingest: missing/invalid content in %s line %d",
                book_id,
                lineno,
            )
            errors += 1
            continue

        section_path = obj.get("section_path")
        if not isinstance(section_path, str):
            section_path = None

        section_hierarchy = obj.get("section_hierarchy")
        if not isinstance(section_hierarchy, str):
            section_hierarchy = None

        image_ref = obj.get("image_ref")
        if not isinstance(image_ref, str):
            image_ref = None

        valid.append(
            {
                "chunk_ref": chunk_ref,
                "content": content,
                "section_path": section_path,
                "section_hierarchy": section_hierarchy,
                "image_ref": image_ref,
            }
        )
    return valid, errors


def _embed_batches(rows: list, char_budget: int, max_count: int):
    """Yield sub-lists of ``rows`` bounded by both summed content length and item
    count, so each embedding request finishes within the client read timeout
    regardless of how large individual chunks are. A single row whose content
    already exceeds ``char_budget`` is yielded alone rather than dropped.
    """
    batch: list = []
    size = 0
    for row in rows:
        clen = len(row.content or "")
        if batch and (size + clen > char_budget or len(batch) >= max_count):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += clen
    if batch:
        yield batch


def _chunks_missing_embedding(session: Session, book_ids, model: str) -> list:
    """Chunks in ``book_ids`` with no embedding row for ``model`` yet.

    Self-heal for a prior partial embed failure: such chunks are unchanged on a
    re-run so ``_upsert_book_chunks`` never re-queues them, yet they still have no
    vector. Selecting them here lets a plain re-run finish an interrupted embed.
    """
    if not book_ids:
        return []
    embedding_exists = (
        select(Embedding.embeddable_id)
        .where(
            Embedding.embeddable_kind == "chunk",
            Embedding.embeddable_id == KnowledgeChunk.id,
            Embedding.model == model,
        )
        .exists()
    )
    return list(
        session.execute(
            select(KnowledgeChunk).where(
                KnowledgeChunk.book_id.in_(list(book_ids)),
                ~embedding_exists,
            )
        )
        .scalars()
        .all()
    )


def _list_manifest_keys(s3_client, bucket: str, prefix: str) -> list[str]:
    """List chunk NDJSON manifest keys under ``prefix``, following pagination.

    Layout is ``<prefix><book_id>/chunks/*.ndjson`` (default prefix ``books/``),
    so only ``.ndjson`` keys under a ``/chunks/`` segment are manifests; a stray
    ``.ndjson`` elsewhere under a book (e.g. in ``raw/``) is ignored.
    """
    keys: list[str] = []
    continuation_token: str | None = None
    while True:
        kwargs: dict[str, str] = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith(_MANIFEST_SUFFIX) and _CHUNKS_SEGMENT in key:
                keys.append(key)
        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break
    return keys


def _book_id_from_key(key: str, prefix: str) -> str:
    """Book id is the path segment between ``prefix`` and ``/chunks/``.

    e.g. ``books/monster-manual/chunks/chunks.ndjson`` -> ``monster-manual``.
    """
    rest = key[len(prefix) :] if key.startswith(prefix) else key
    return rest.split(_CHUNKS_SEGMENT, 1)[0].split("/", 1)[0]


def _upsert_book(session: Session, book_id: str) -> None:
    """Ensure a grimoire.book row exists for ``book_id`` (display_name defaults
    to the id until renamed from the Library UI). Never overwrites an existing
    display_name: a rename must survive re-uploads. Own savepoint so a book-row
    failure cannot roll back the chunk upserts that follow.
    """
    with session.begin_nested():
        if session.get(Book, book_id) is None:
            session.add(
                Book(
                    id=book_id,
                    display_name=_default_display_name(book_id),
                    copyrighted_content=book_id not in OPEN_LICENSE_BOOK_IDS,
                )
            )


def _upsert_book_chunks(
    session: Session, book_id: str, parsed: list[dict]
) -> tuple[list[KnowledgeChunk], int]:
    """Sync upsert of one book's parsed chunks, keyed on (book_id, chunk_ref).

    Inserts new chunks, updates changed content/section_path/image_ref, and
    keeps ``seq`` in lockstep with NDJSON line order (the ``parsed`` list is in
    manifest order). seq is rewritten on every run, so a re-upload that reorders
    or inserts lines produces correct reading order even for chunks whose
    content is otherwise unchanged. A pure seq move does not touch the vector.
    Each chunk gets its own savepoint so one bad row cannot roll back the rest
    of the batch. Returns the rows that need (re-)embedding and how many chunks
    were upserted (inserted or content/metadata-changed; a seq-only shift does
    not count as an upsert).
    """
    pending_embed: list[KnowledgeChunk] = []
    upserted = 0
    for seq, item in enumerate(parsed):
        with session.begin_nested():
            existing = session.execute(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.book_id == book_id,
                    KnowledgeChunk.chunk_ref == item["chunk_ref"],
                )
            ).scalar_one_or_none()

            if existing is None:
                row = KnowledgeChunk(
                    book_id=book_id,
                    chunk_ref=item["chunk_ref"],
                    content=item["content"],
                    section_path=item["section_path"],
                    section_hierarchy=item["section_hierarchy"],
                    image_ref=item["image_ref"],
                    seq=seq,
                )
                session.add(row)
                session.flush()
                upserted += 1
                pending_embed.append(row)
            else:
                content_changed = existing.content != item["content"]
                # section_hierarchy is extraction-context metadata (not embedded),
                # so a change to it is a metadata-only upsert like section_path:
                # it updates in place and does NOT re-embed. This is what makes the
                # v2 hierarchy backfill a cheap metadata reload, not a 40k re-embed.
                #
                # A None hierarchy in the NDJSON must NOT clobber a stored
                # breadcrumb: the original books' NDJSONs predate hierarchy baking,
                # so a scheduled load-chunks re-run would silently erase the whole
                # hierarchy backfill (it did, 2026-07-06, mid extraction run). The
                # stored value wins until a re-uploaded NDJSON actually carries a
                # hierarchy of its own.
                incoming_hierarchy = item["section_hierarchy"]
                if incoming_hierarchy is None:
                    incoming_hierarchy = existing.section_hierarchy
                meta_changed = (
                    existing.section_path != item["section_path"]
                    or existing.section_hierarchy != incoming_hierarchy
                    or existing.image_ref != item["image_ref"]
                )
                # seq is corrected on every run but is not itself an "upsert":
                # reading order shifting on a re-upload should not report the
                # whole book as changed nor trigger re-embedding.
                if existing.seq != seq:
                    existing.seq = seq
                if content_changed or meta_changed:
                    existing.content = item["content"]
                    existing.section_path = item["section_path"]
                    existing.section_hierarchy = incoming_hierarchy
                    existing.image_ref = item["image_ref"]
                    upserted += 1
                    # Only re-embed when the embedded text (content) changed; a
                    # metadata-only fix does not move the vector.
                    if content_changed:
                        pending_embed.append(existing)
                # else: content/metadata unchanged (seq may have been corrected).
    session.commit()
    return pending_embed, upserted


def upsert_embedding_batch(
    session: Session,
    model: str,
    embeddable_kind: str,
    rows: list,
    vectors: list[list[float]],
) -> int:
    """Sync upsert of one batch's embedding rows. Returns the count embedded.

    Public (not module-private) because it is shared across every
    embeddable kind: chunks here, entities in ``extract.py``. ``rows`` only
    needs an ``.id`` attribute, so it accepts any embeddable row type.
    ``model``/``dim`` are recorded from the embed call itself (``model`` is
    the client's own model name, ``dim`` is ``len(vector)``), not a separate
    hardcoded constant. Each row gets its own savepoint, mirroring
    ``_upsert_book_chunks``.
    """
    embedded = 0
    for row, vector in zip(rows, vectors, strict=True):
        with session.begin_nested():
            existing = session.execute(
                select(Embedding).where(
                    Embedding.embeddable_kind == embeddable_kind,
                    Embedding.embeddable_id == row.id,
                    Embedding.model == model,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Embedding(
                        embeddable_kind=embeddable_kind,
                        embeddable_id=row.id,
                        model=model,
                        dim=len(vector),
                        vector=vector,
                    )
                )
            else:
                existing.vector = vector
                existing.dim = len(vector)
            embedded += 1
    session.commit()
    return embedded


async def load_chunks(
    session: Session,
    s3_client,
    embed_client: _Embedder,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
) -> dict:
    """List, parse, and upsert every book manifest under ``prefix``.

    Lists and reads manifests from S3, parses them, and delegates the
    Session I/O to sync helpers (see module docstring); the embed call is
    the only ``await`` in this function.

    Returns ``{"books", "chunks_upserted", "chunks_embedded", "errors"}``.
    """
    keys = _list_manifest_keys(s3_client, bucket, prefix)

    chunks_upserted = 0
    chunks_embedded = 0
    errors = 0
    pending_embed: list[KnowledgeChunk] = []

    # Concatenate every manifest belonging to a book before upserting, so ``seq``
    # is assigned once over the book's full chunk sequence. A book may span
    # several ``<book_id>/chunks/*.ndjson`` files (see _list_manifest_keys); if
    # each file were enumerated independently, seq would restart at 0 per file
    # and collide within the book, breaking the reading-order reads in library.py
    # (keyset pagination, prev/next, section order) that assume seq is unique per
    # book. Keys are sorted so multi-file books have a deterministic order
    # (e.g. chunks-001, chunks-002).
    parsed_by_book: dict[str, list[dict]] = {}
    for key in sorted(keys):
        book_id = _book_id_from_key(key, prefix)
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        parsed, err_count = parse_manifest_lines(book_id, body.splitlines())
        errors += err_count
        parsed_by_book.setdefault(book_id, []).extend(parsed)

    books = len(parsed_by_book)
    book_ids: set[str] = set(parsed_by_book)

    for book_id, parsed in parsed_by_book.items():
        _upsert_book(session, book_id)
        book_pending, book_upserted = _upsert_book_chunks(session, book_id, parsed)
        chunks_upserted += book_upserted
        pending_embed.extend(book_pending)

    # Also (re-)embed chunks that exist but have no vector for this model, e.g.
    # left behind when a prior run's embed step failed partway. Dedupe against
    # the rows already queued from this run's upserts.
    queued = {row.id for row in pending_embed}
    for row in _chunks_missing_embedding(session, book_ids, embed_client.model):
        if row.id not in queued:
            pending_embed.append(row)
            queued.add(row.id)

    # Each batch commits its own embeddings (see upsert_embedding_batch), so an
    # interrupted run leaves durable partial progress and the next run's
    # self-heal finishes the rest.
    for batch in _embed_batches(pending_embed, EMBED_CHAR_BUDGET, EMBED_MAX_BATCH):
        vectors = await embed_client.embed_batch(
            [(row.content or "")[:EMBED_INPUT_MAX_CHARS] for row in batch]
        )
        chunks_embedded += upsert_embedding_batch(
            session, embed_client.model, "chunk", batch, vectors
        )

    summary = {
        "books": books,
        "chunks_upserted": chunks_upserted,
        "chunks_embedded": chunks_embedded,
        "errors": errors,
    }
    logger.info("grimoire ingest: %s", summary)
    return summary
