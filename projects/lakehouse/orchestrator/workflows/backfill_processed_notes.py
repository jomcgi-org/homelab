"""BackfillFromProcessedNotesWorkflow — seed the event stream from the KG.

This is the workflow that proves the whole event-sourced lakehouse pipeline
end-to-end (Wavefront-3 success criterion 3). It reads the bounded, well-defined
``_processed`` note corpus straight out of Postgres (``knowledge.notes`` +
``knowledge.chunks``, ~4585 live notes per WAVEFRONT-0-discover §4) and replays
each note as a ``note`` ``created`` domain event onto ``events.knowledge.note``.

Design properties (the ones that matter):

* **One-shot, re-runnable, idempotent.** Every event is published with
  ``Nats-Msg-Id = {note_id}-v1`` (ADR 017 idempotency layer 1), so re-running
  the backfill against the same corpus republishes identical msg-ids and
  JetStream drops the duplicates. The workflow can be killed and restarted with
  no double-effect at the consumer.
* **Embeddings travel in the payload.** Pre-computed voyage-4-nano (1024-dim)
  vectors are read from ``knowledge.chunks.embedding`` and carried in the event
  payload, so serving rebuilds never re-embed (platform/004 §"What does NOT
  change").
* **Read path is raw SQL via psycopg.** No monolith model import — keeps the
  worker image lean and off the ``monolith_backend`` dep graph
  (WAVEFRONT-0-discover §4). The worker pod gets ``monolith-pg`` creds via
  ``DATABASE_URL`` and queries read-only.
* **Deterministic & resumable.** All I/O lives in activities; the workflow body
  only paginates (``offset``/``limit``) and accumulates a count. Past a safe
  history size it ``continue_as_new``s so the full 4585-note run never bloats a
  single workflow history (ADR 015 §Worker pod lifecycle / durability).

The workflow ID is set by the caller (deterministic, e.g.
``backfill-processed-notes``) so a re-trigger dedups via Temporal's
``WorkflowAlreadyStartedError`` semantics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

# Heavy / non-deterministic libraries are imported inside the workflow's
# deterministic sandbox only through ``imports_passed_through`` so the SDK does
# not flag them as non-deterministic. Activities import them normally at module
# scope (they run outside the sandbox).
with workflow.unsafe.imports_passed_through():
    from projects.lakehouse.events.envelope import build_envelope
    from projects.lakehouse.events.publish import publish_event, subject_for
    from projects.lakehouse.events.registry.note import (
        NoteChunk,
        NoteCreatedPayload,
    )
    from projects.lakehouse.nats_client.client import NatsClient

# --- constants ------------------------------------------------------------

# entity_type for the backfill events. ADR 017 / events.publish SUBJECT_BY_ENTITY.
ENTITY_TYPE = "note"
# All backfilled notes are the genesis (v1) event for their entity.
BACKFILL_EVENT_VERSION = 1
# Producer identity recorded in the envelope (ADR 017 §envelope fields).
PRODUCER = "lakehouse.backfill"

# How many notes a single ``read_processed_notes_batch`` activity reads. Bounded
# so the activity stays well under NATS' 1MiB message size when the batch is
# published, and so a retry replays a small unit of work.
DEFAULT_BATCH_SIZE = 50

# After this many notes processed within one workflow run, ``continue_as_new``
# to truncate the event history. The full 4585-note corpus spans ~92 batches at
# the default size; resetting history every few hundred keeps each run's history
# small and replay cheap (ADR 015 risk: workflow history bloat).
DEFAULT_CONTINUE_AS_NEW_THRESHOLD = 500


@dataclass
class BackfillInput:
    """Resumable input for the backfill workflow.

    ``offset`` is where this run picks up; ``processed`` carries the running
    total across ``continue_as_new`` boundaries so the final return value counts
    the whole corpus, not just the last run's slice.
    """

    offset: int = 0
    batch_size: int = DEFAULT_BATCH_SIZE
    processed: int = 0
    continue_as_new_threshold: int = DEFAULT_CONTINUE_AS_NEW_THRESHOLD


# --- activities -----------------------------------------------------------


def _resolve_database_url() -> str:
    """Read ``DATABASE_URL`` for the read-only backfill query.

    The worker pod is injected with ``monolith-pg`` credentials. Normalizes the
    SQLAlchemy ``postgresql+psycopg://`` dialect prefix (used by the monolith)
    back to a plain ``postgresql://`` libpq URL that psycopg.connect accepts.
    """
    url = os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        raise RuntimeError(
            "DATABASE_URL is required for read_processed_notes_batch "
            "(monolith-pg read-only credentials)"
        )
    return url.strip().replace("postgresql+psycopg://", "postgresql://", 1)


@activity.defn
async def read_processed_notes_batch(offset: int, limit: int) -> list[dict]:
    """Read one page of live ``_processed`` notes (+ their chunks) via raw SQL.

    Read-only. Pages ``knowledge.notes`` by ``id`` (stable order) where
    ``path LIKE '_processed/%'`` and ``deleted_at IS NULL`` (the corpus is
    defined in WAVEFRONT-0-discover §4), LEFT JOINs ``knowledge.chunks`` to
    assemble per-note chunk lists (``chunk_index``, ``section_header``,
    ``chunk_text``, ``embedding`` cast to ``real[]`` so psycopg returns a plain
    list rather than a pgvector string), and returns plain JSON-serializable
    dicts (Temporal activity results must be serializable).

    psycopg is imported here (inside the activity, outside the workflow sandbox)
    per the deterministic-workflow rule.
    """
    import psycopg

    dsn = _resolve_database_url()

    # Two-step read: page the note ids first (deterministic LIMIT/OFFSET over a
    # single table), then fetch all chunks for that page in one query. This
    # avoids a row-multiplying JOIN's LIMIT/OFFSET ambiguity and keeps the chunk
    # fetch a single round-trip.
    notes_by_id: dict[int, dict] = {}
    ordered_ids: list[int] = []

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, note_id, path, title, content_hash,
                       type, status, visibility, tags, aliases
                FROM knowledge.notes
                WHERE path LIKE '_processed/%%'
                  AND deleted_at IS NULL
                ORDER BY id
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {"limit": limit, "offset": offset},
            )
            for row in cur.fetchall():
                (
                    note_pk,
                    note_id,
                    path,
                    title,
                    content_hash,
                    note_type,
                    status,
                    visibility,
                    tags,
                    aliases,
                ) = row
                ordered_ids.append(note_pk)
                notes_by_id[note_pk] = {
                    "note_id": note_id,
                    "path": path,
                    "title": title or "",
                    "content_hash": content_hash or "",
                    "type": note_type or "",
                    "status": status or "",
                    # visibility is nullable in the schema; NoteCreatedPayload
                    # wants a str, so normalize NULL to "" here.
                    "visibility": visibility or "",
                    "tags": list(tags) if tags else [],
                    "aliases": list(aliases) if aliases else [],
                    "chunks": [],
                }

            if ordered_ids:
                cur.execute(
                    """
                    SELECT note_fk, chunk_index, section_header,
                           chunk_text, embedding::real[]
                    FROM knowledge.chunks
                    WHERE note_fk = ANY(%(ids)s)
                    ORDER BY note_fk, chunk_index
                    """,
                    {"ids": ordered_ids},
                )
                for (
                    note_fk,
                    chunk_index,
                    section_header,
                    chunk_text,
                    embedding,
                ) in cur.fetchall():
                    note = notes_by_id.get(note_fk)
                    if note is None:
                        continue
                    note["chunks"].append(
                        {
                            "chunk_index": chunk_index,
                            "section_header": section_header,
                            "chunk_text": chunk_text or "",
                            "embedding": (
                                [float(x) for x in embedding] if embedding else []
                            ),
                        }
                    )

    # Preserve the paged id order in the returned list.
    return [notes_by_id[note_pk] for note_pk in ordered_ids]


@activity.defn
async def publish_note_events(batch: list[dict]) -> int:
    """Publish each note in ``batch`` as a ``note`` ``created`` domain event.

    For each note: build a validated :class:`NoteCreatedPayload` (embeddings
    included), wrap it in an ADR-017 :class:`EventEnvelope`
    (``entity_type='note'``, ``event_type='created'``, ``event_version=1``,
    ``entity_id=note_id``, ``producer='lakehouse.backfill'``), and publish to
    ``events.knowledge.note`` via the NATS JetStream client. The publish helper
    sets ``Nats-Msg-Id = {note_id}-v1`` so a re-run dedups (ADR 017).

    Returns the number of events published. The NATS client and event imports
    happen inside the activity (outside the workflow sandbox).
    """
    if not batch:
        return 0

    nats_client = NatsClient()
    await nats_client.connect()
    published = 0
    try:
        for note in batch:
            chunks = [
                NoteChunk(
                    chunk_index=chunk["chunk_index"],
                    section_header=chunk.get("section_header"),
                    chunk_text=chunk["chunk_text"],
                    embedding=chunk.get("embedding", []),
                )
                for chunk in note.get("chunks", [])
            ]
            payload = NoteCreatedPayload(
                note_id=note["note_id"],
                path=note["path"],
                title=note["title"],
                content_hash=note["content_hash"],
                type=note["type"],
                status=note["status"],
                visibility=note["visibility"],
                tags=note.get("tags", []),
                aliases=note.get("aliases", []),
                chunks=chunks,
            )
            envelope = build_envelope(
                entity_type=ENTITY_TYPE,
                entity_id=note["note_id"],
                event_type="created",
                event_version=BACKFILL_EVENT_VERSION,
                producer=PRODUCER,
                payload=payload.model_dump(),
            )
            # subject_for resolves events.knowledge.note from entity_type; the
            # publish helper derives the dedup msg-id ({note_id}-v1) internally.
            await publish_event(nats_client, envelope, subject=subject_for(envelope))
            published += 1
    finally:
        await nats_client.close()

    return published


# --- workflow -------------------------------------------------------------


@workflow.defn
class BackfillFromProcessedNotesWorkflow:
    """One-shot, re-runnable, idempotent backfill of the ``_processed`` corpus.

    Loops over pages of notes, publishing each note's genesis event, until a
    short page signals the corpus is exhausted. ``continue_as_new`` past a safe
    history size keeps the full ~4585-note run replay-cheap. Returns the total
    notes processed across all ``continue_as_new`` segments.
    """

    @workflow.run
    async def run(self, params: BackfillInput | None = None) -> int:
        params = params or BackfillInput()
        offset = params.offset
        processed = params.processed
        # Notes processed within THIS run segment; resets on continue_as_new.
        processed_this_run = 0

        read_retry = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
            maximum_attempts=5,
        )
        publish_retry = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
            maximum_attempts=5,
        )

        while True:
            batch = await workflow.execute_activity(
                read_processed_notes_batch,
                args=[offset, params.batch_size],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=read_retry,
            )
            if not batch:
                break

            published = await workflow.execute_activity(
                publish_note_events,
                args=[batch],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=publish_retry,
            )
            processed += published
            processed_this_run += published
            offset += len(batch)

            # A short page (fewer than requested) means we've reached the end of
            # the corpus — stop without another (empty) read.
            if len(batch) < params.batch_size:
                break

            # Truncate history for the long full-corpus run by carrying the
            # cursor + running total into a fresh workflow execution.
            if processed_this_run >= params.continue_as_new_threshold:
                workflow.continue_as_new(
                    args=[
                        BackfillInput(
                            offset=offset,
                            batch_size=params.batch_size,
                            processed=processed,
                            continue_as_new_threshold=params.continue_as_new_threshold,
                        )
                    ]
                )

        return processed


# Auto-discovery exports (read by the workflows package loader).
WORKFLOWS = [BackfillFromProcessedNotesWorkflow]
ACTIVITIES = [read_processed_notes_batch, publish_note_events]
