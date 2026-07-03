"""S3 chunk loader: NDJSON manifests -> knowledge_chunk + embedding.

Spec #4.1/#4.2 (docs/plans/2026-07-02-grimoire-pg-first-spec.md): an external
chunking service drops per-book NDJSON manifests at
``s3://<bucket>/<prefix><book_id>.ndjson``, one chunk object per line. This
module lists those manifests, upserts rows into ``knowledge_chunk`` keyed on
``(book_id, chunk_ref)``, and embeds new/changed content into the generic
``embedding`` table (``embeddable_kind="chunk"``).

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

from grimoire.models import Embedding, KnowledgeChunk

logger = logging.getLogger("monolith.grimoire.ingest")

DEFAULT_PREFIX = "chunks/"
_MANIFEST_SUFFIX = ".ndjson"

# Embeddings are sent in fixed-size batches rather than one call per book, so
# a large manifest does not force one oversized HTTP request (mirrors the
# spirit of knowledge.indexing, which batches per note; here the natural
# batch is per-run rather than per-book).
EMBED_BATCH_SIZE = 64


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
    Optional: ``section_path`` (str, else dropped); ``meta`` and any other
    field is ignored. A line that is invalid JSON, not an object, or missing
    a valid required field is counted as an error and logged at warning
    (never raised) so one bad line never fails the batch.

    Returns ``(valid_chunks, error_count)`` where each valid chunk is
    ``{"chunk_ref", "content", "section_path"}``.
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

        valid.append(
            {"chunk_ref": chunk_ref, "content": content, "section_path": section_path}
        )
    return valid, errors


def _list_manifest_keys(s3_client, bucket: str, prefix: str) -> list[str]:
    """List NDJSON manifest keys under ``prefix``, following pagination."""
    keys: list[str] = []
    continuation_token: str | None = None
    while True:
        kwargs: dict[str, str] = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith(_MANIFEST_SUFFIX):
                keys.append(key)
        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break
    return keys


def _book_id_from_key(key: str, prefix: str) -> str:
    name = key[len(prefix) :] if key.startswith(prefix) else key.rsplit("/", 1)[-1]
    return name.removesuffix(_MANIFEST_SUFFIX)


def _upsert_book_chunks(
    session: Session, book_id: str, parsed: list[dict]
) -> tuple[list[KnowledgeChunk], int]:
    """Sync upsert of one book's parsed chunks, keyed on (book_id, chunk_ref).

    Inserts new chunks, updates changed content/section_path, and skips
    unchanged chunks entirely (so a second run over identical data embeds
    nothing). Each chunk gets its own savepoint so one bad row cannot roll
    back the rest of the batch. Returns the rows that need (re-)embedding
    and how many chunks were upserted (inserted or changed).
    """
    pending_embed: list[KnowledgeChunk] = []
    upserted = 0
    for item in parsed:
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
                )
                session.add(row)
                session.flush()
                upserted += 1
                pending_embed.append(row)
            elif existing.content != item["content"]:
                existing.content = item["content"]
                existing.section_path = item["section_path"]
                upserted += 1
                pending_embed.append(existing)
            # else: unchanged, skip entirely (idempotent re-run).
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

    books = 0
    chunks_upserted = 0
    chunks_embedded = 0
    errors = 0
    pending_embed: list[KnowledgeChunk] = []

    for key in keys:
        book_id = _book_id_from_key(key, prefix)
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        parsed, err_count = parse_manifest_lines(book_id, body.splitlines())
        errors += err_count
        books += 1

        book_pending, book_upserted = _upsert_book_chunks(session, book_id, parsed)
        chunks_upserted += book_upserted
        pending_embed.extend(book_pending)

    for start in range(0, len(pending_embed), EMBED_BATCH_SIZE):
        batch = pending_embed[start : start + EMBED_BATCH_SIZE]
        vectors = await embed_client.embed_batch([row.content for row in batch])
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
