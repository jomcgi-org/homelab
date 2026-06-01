"""ExportNoteChangesWorkflow — incremental add/update/soft-delete forwarder.

The bootstrap (:mod:`backfill_processed_notes`) seeds the event log from the
whole ``_processed`` corpus once. This workflow keeps the lakehouse *fresh*
afterwards by forwarding only what changed, so it can run on a cheap cadence
(daily/hourly) instead of re-exporting everything.

How it stays correct without hooking the filesystem (ADR 016/017):

* **PG is the change chokepoint.** However a note's markdown is edited, the
  monolith's ingest converges it into ``knowledge.notes``: it recomputes
  ``content_hash``, re-embeds into ``knowledge.chunks``, and — atomically, in one
  transaction (``store.upsert_note``) — stamps ``indexed_at = now()``. Unchanged
  content is skipped, so ``indexed_at`` advances *iff* content actually changed.
  Soft deletes stamp ``deleted_at = now()`` and keep the row. We therefore poll a
  single monotonic per-row marker, ``change_at = GREATEST(indexed_at, deleted_at)``.
* **One snapshot per read.** The note page and its chunks are read in a single
  ``REPEATABLE READ`` transaction, so a concurrent upsert can never tear a note's
  metadata from its embeddings — they were committed together and we see one
  consistent snapshot.
* **Source-derived, idempotent version.** ``event_version`` is the epoch-millis of
  ``change_at`` (int64). It is a pure function of the source row, so an activity
  retry or the watermark re-scan margin re-emits the *same* ``{note_id}-v{ms}``
  dedup key — JetStream drops it inside the dedup window, and the serving fold
  (now deduped per ``(note_id, event_version, chunk_index)``) collapses any copy
  that slips past it. Monotonic per note, so the fold's ``MAX(event_version)``
  always picks the latest revision; a tombstone (versioned on ``deleted_at``)
  sorts after the content it supersedes.
* **Watermark in the lakehouse DB.** A one-row ``export_state`` table (in the
  lakehouse Postgres database that already backs the Iceberg catalog — never the
  monolith's ``knowledge`` schema) records the high-water ``change_at``. Re-reads
  start at ``watermark - margin`` so a transaction that committed late (below the
  prior high-water mark) is caught next run; idempotency makes the overlap free.

Scope: this forwards **adds, updates, and soft deletes**. *Hard* deletes
(``store.delete_note`` physically removes the row) can't be forwarded — there is
nothing left to read — and need a periodic reconcile pass (diff live PG note_ids
against the served set, tombstone the difference). That's a documented follow-up.

The schedule (``schedules/note_export``) runs this with ``overlap=SKIP`` and a
stable workflow id, so runs never interleave and the watermark advance is
effectively single-threaded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from projects.lakehouse.events.envelope import build_envelope
    from projects.lakehouse.events.publish import publish_event, subject_for
    from projects.lakehouse.events.registry.note import (
        NoteChunk,
        NoteTombstonedPayload,
        NoteUpdatedPayload,
    )
    from projects.lakehouse.nats_client.client import NatsClient

# --- constants ------------------------------------------------------------

ENTITY_TYPE = "note"
PRODUCER = "lakehouse.export"
# Watermark row key in lakehouse.export_state (one row per logical export).
WATERMARK_NAME = "note-export"
# Lakehouse catalog DB name (matches iceberg.catalog DEFAULT_CATALOG_DB); the
# watermark lives here, not in the monolith's knowledge schema.
DEFAULT_CATALOG_DB = "lakehouse"

DEFAULT_BATCH_SIZE = 50
# Re-scan window (seconds) below the stored watermark — covers transactions that
# were assigned change_at = now() but committed after a prior run snapshotted,
# so they'd otherwise sit invisibly under the high-water mark. Idempotency makes
# re-reading this window harmless.
DEFAULT_MARGIN_SECONDS = 300
DEFAULT_CONTINUE_AS_NEW_THRESHOLD = 500


@dataclass
class ExportInput:
    """Resumable input for the incremental export.

    ``lower_bound`` (the fixed ``watermark - margin`` floor for this run) and the
    keyset cursor ``(cursor_change_at, cursor_id)`` carry across
    ``continue_as_new`` so a long delta paginates without re-reading. ``processed``
    is the running total; ``last_change_at`` is the high-water ``change_at`` seen
    so far, written back to the watermark when the run completes.
    """

    lower_bound: str | None = None
    cursor_change_at: str | None = None
    cursor_id: int = 0
    processed: int = 0
    last_change_at: str | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    margin_seconds: int = DEFAULT_MARGIN_SECONDS
    continue_as_new_threshold: int = DEFAULT_CONTINUE_AS_NEW_THRESHOLD


# --- db url helpers -------------------------------------------------------


def _resolve_knowledge_db_url() -> str:
    """Read ``DATABASE_URL`` (monolith-pg) as a plain libpq URL for psycopg."""
    url = os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        raise RuntimeError("DATABASE_URL is required for the note export")
    return url.strip().replace("postgresql+psycopg://", "postgresql://", 1)


def _resolve_lakehouse_db_url() -> str:
    """Derive the lakehouse-DB libpq URL from ``DATABASE_URL``.

    Reuses the monolith-pg credentials against the dedicated ``lakehouse``
    database (``ICEBERG_CATALOG_DB``, default ``lakehouse``) — the same database
    the Iceberg SqlCatalog uses — so the watermark never touches the monolith's
    ``knowledge`` schema. Mirrors ``iceberg.catalog._catalog_uri`` but returns a
    plain ``postgresql://`` URL psycopg.connect accepts.
    """
    base = _resolve_knowledge_db_url()
    db = os.environ.get("ICEBERG_CATALOG_DB", DEFAULT_CATALOG_DB)
    parts = urlsplit(base)
    return urlunsplit(parts._replace(path=f"/{db}"))


# --- activities -----------------------------------------------------------

_CREATE_STATE_SQL = """
CREATE TABLE IF NOT EXISTS export_state (
    name       text PRIMARY KEY,
    watermark  timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)
""".strip()

# Monotonic per-row change marker: the later of (re)index and soft-delete.
_CHANGE_AT = "GREATEST(indexed_at, COALESCE(deleted_at, indexed_at))"

# Corpus membership. A row belongs to the export if it is a live ``_processed``
# note OR a soft-deleted note that WAS in ``_processed`` before the monolith's
# ``delete_note`` rewrote its path to ``_trash/`` (it stashes the original in
# ``pre_delete_path``). The pre_delete_path branch is load-bearing: a soft delete
# stamps ``deleted_at`` *and* moves the path out of ``_processed/`` in the same
# commit, so gating only on ``path LIKE '_processed/%'`` would silently never
# emit a tombstone and the deleted note's vectors would stay indexed forever.
# ``%%`` (parameterized queries only — psycopg3 unescapes to a single ``%``).
_CORPUS_MEMBER = (
    "((deleted_at IS NULL AND path LIKE '_processed/%%') "
    "OR (deleted_at IS NOT NULL AND pre_delete_path LIKE '_processed/%%'))"
)


@activity.defn
async def read_export_lower_bound(name: str, margin_seconds: int) -> str:
    """Return the inclusive lower bound (ISO) for this run: ``watermark - margin``.

    Ensures ``export_state`` exists. On first sight (no row), initializes the
    watermark to the *current* max ``change_at`` in the corpus — i.e. it assumes
    the one-shot bootstrap already seeded history, so the first incremental run is
    a near-no-op rather than a redundant full re-export. (To force a full
    re-export, ``UPDATE export_state SET watermark = '1970-01-01' WHERE name=…``.)
    """
    import psycopg

    with psycopg.connect(_resolve_lakehouse_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_STATE_SQL)
            cur.execute(
                "SELECT watermark FROM export_state WHERE name = %(name)s",
                {"name": name},
            )
            row = cur.fetchone()
            if row is not None:
                watermark = row[0]
            else:
                # Initialize from the corpus's current high-water change_at.
                with psycopg.connect(_resolve_knowledge_db_url()) as kconn:
                    with kconn.cursor() as kcur:
                        # No params on this execute, so psycopg3 does NOT unescape
                        # %% -> % here; use single % directly. Mirrors _CORPUS_MEMBER
                        # so the seed watermark accounts for soft-deletes too.
                        kcur.execute(
                            f"SELECT MAX({_CHANGE_AT}) FROM knowledge.notes "
                            "WHERE (deleted_at IS NULL AND path LIKE '_processed/%') "
                            "OR (deleted_at IS NOT NULL "
                            "AND pre_delete_path LIKE '_processed/%')"
                        )
                        watermark = kcur.fetchone()[0] or datetime(
                            1970, 1, 1, tzinfo=timezone.utc
                        )
                cur.execute(
                    "INSERT INTO export_state (name, watermark) "
                    "VALUES (%(name)s, %(watermark)s) "
                    "ON CONFLICT (name) DO NOTHING",
                    {"name": name, "watermark": watermark},
                )
        conn.commit()

    lower = watermark - timedelta(seconds=margin_seconds)
    return lower.isoformat()


@activity.defn
async def read_note_changes_batch(
    lower_bound: str,
    cursor_change_at: str | None,
    cursor_id: int,
    limit: int,
) -> list[dict]:
    """Read one keyset page of changed ``_processed`` notes since ``lower_bound``.

    Single ``REPEATABLE READ`` snapshot: pages by ``(change_at, id)`` so the scan
    is stable under concurrent inserts, then fetches chunks for the *live* notes
    in the page in one round-trip. Soft-deleted notes (``deleted_at IS NOT NULL``)
    are returned as tombstones (no body/embeddings — ADR 017 RTBF); live notes
    carry their chunks. Each row is tagged with the ``event_type`` and the
    source-derived ``event_version`` (epoch-millis of its ``change_at``).
    """
    import psycopg

    cursor_ts = cursor_change_at if cursor_change_at is not None else lower_bound

    ordered_pks: list[int] = []
    live_ids: list[int] = []
    by_pk: dict[int, dict] = {}

    with psycopg.connect(_resolve_knowledge_db_url()) as conn:
        # One snapshot for both queries so a note's metadata and its chunk
        # embeddings (committed together by upsert_note) can't be torn apart.
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, note_id, path, title, content_hash, type, status,
                       visibility, tags, aliases, indexed_at, deleted_at,
                       {_CHANGE_AT} AS change_at
                FROM knowledge.notes
                WHERE {_CORPUS_MEMBER}
                  -- psycopg3 sends str params as text; cast so the comparison is
                  -- timestamptz>timestamptz, not the missing timestamptz>text op.
                  AND {_CHANGE_AT} > %(lower)s::timestamptz
                  AND ({_CHANGE_AT}, id) > (%(cursor_ts)s::timestamptz, %(cursor_id)s)
                ORDER BY change_at, id
                LIMIT %(limit)s
                """,
                {
                    "lower": lower_bound,
                    "cursor_ts": cursor_ts,
                    "cursor_id": cursor_id,
                    "limit": limit,
                },
            )
            for row in cur.fetchall():
                (
                    pk,
                    note_id,
                    path,
                    title,
                    content_hash,
                    note_type,
                    status,
                    visibility,
                    tags,
                    aliases,
                    indexed_at,
                    deleted_at,
                    change_at,
                ) = row
                ordered_pks.append(pk)
                # event_version = epoch-millis of the change marker (int64).
                version = int(change_at.timestamp() * 1000)
                if deleted_at is not None:
                    by_pk[pk] = {
                        "note_id": note_id,
                        "event_type": "tombstoned",
                        "event_version": version,
                        "change_at": change_at.isoformat(),
                        "id": pk,
                    }
                else:
                    live_ids.append(pk)
                    by_pk[pk] = {
                        "note_id": note_id,
                        "path": path,
                        "title": title or "",
                        "content_hash": content_hash or "",
                        "type": note_type or "",
                        "status": status or "",
                        "visibility": visibility or "",
                        "tags": list(tags) if tags else [],
                        "aliases": list(aliases) if aliases else [],
                        "chunks": [],
                        "event_type": "updated",
                        "event_version": version,
                        "change_at": change_at.isoformat(),
                        "id": pk,
                    }

            if live_ids:
                cur.execute(
                    """
                    SELECT note_fk, chunk_index, section_header,
                           chunk_text, embedding::real[]
                    FROM knowledge.chunks
                    WHERE note_fk = ANY(%(ids)s)
                    ORDER BY note_fk, chunk_index
                    """,
                    {"ids": live_ids},
                )
                for (
                    note_fk,
                    chunk_index,
                    section_header,
                    chunk_text,
                    embedding,
                ) in cur.fetchall():
                    note = by_pk.get(note_fk)
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

    # Return in the SQL's (change_at, id) order — trust the server's timestamptz
    # ordering rather than re-deriving it from ISO strings (which would mis-sort
    # under a non-UTC session timezone), so the workflow's batch[-1] cursor is
    # always the true page maximum.
    return [by_pk[pk] for pk in ordered_pks]


@activity.defn
async def publish_note_change_events(batch: list[dict]) -> int:
    """Publish each changed note as its ``updated`` / ``tombstoned`` domain event.

    Reuses the ADR-017 envelope + NATS publisher. ``event_type`` and
    ``event_version`` come from the row (set by :func:`read_note_changes_batch`),
    so the dedup key ``{note_id}-v{version}`` is source-derived and idempotent.
    Tombstones carry only the note id (no body/embeddings).
    """
    if not batch:
        return 0

    nats_client = NatsClient()
    await nats_client.connect()
    published = 0
    try:
        for note in batch:
            event_type = note["event_type"]
            if event_type == "tombstoned":
                payload = NoteTombstonedPayload(note_id=note["note_id"])
            else:
                chunks = [
                    NoteChunk(
                        chunk_index=chunk["chunk_index"],
                        section_header=chunk.get("section_header"),
                        chunk_text=chunk["chunk_text"],
                        embedding=chunk.get("embedding", []),
                    )
                    for chunk in note.get("chunks", [])
                ]
                payload = NoteUpdatedPayload(
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
                event_type=event_type,
                event_version=note["event_version"],
                producer=PRODUCER,
                payload=payload.model_dump(),
            )
            await publish_event(nats_client, envelope, subject=subject_for(envelope))
            published += 1
    finally:
        await nats_client.close()

    return published


@activity.defn
async def advance_export_watermark(name: str, watermark: str) -> None:
    """Set ``export_state.watermark`` to ``watermark`` (monotonically).

    Uses ``GREATEST`` so a stale/replayed advance can never move the watermark
    backwards. Called once per run after all batches publish, so a mid-run crash
    leaves the watermark untouched and the next run safely re-reads (idempotent).
    """
    import psycopg

    with psycopg.connect(_resolve_lakehouse_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_STATE_SQL)
            cur.execute(
                """
                INSERT INTO export_state (name, watermark)
                VALUES (%(name)s, %(watermark)s::timestamptz)
                ON CONFLICT (name) DO UPDATE
                SET watermark = GREATEST(export_state.watermark, EXCLUDED.watermark),
                    updated_at = now()
                """,
                {"name": name, "watermark": watermark},
            )
        conn.commit()


# --- workflow -------------------------------------------------------------


@workflow.defn
class ExportNoteChangesWorkflow:
    """Forward note adds/updates/soft-deletes changed since the watermark.

    Resolves ``lower_bound = watermark - margin`` once, keyset-paginates the
    changed set, publishes each page, and advances the watermark to the highest
    ``change_at`` processed. ``continue_as_new`` keeps a large delta replay-cheap.
    Returns the number of events forwarded across all segments.
    """

    @workflow.run
    async def run(self, params: ExportInput | None = None) -> int:
        params = params or ExportInput()

        retry = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
            maximum_attempts=5,
        )

        lower_bound = params.lower_bound
        if lower_bound is None:
            lower_bound = await workflow.execute_activity(
                read_export_lower_bound,
                args=[WATERMARK_NAME, params.margin_seconds],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=retry,
            )

        cursor_change_at = params.cursor_change_at
        cursor_id = params.cursor_id
        processed = params.processed
        last_change_at = params.last_change_at
        processed_this_run = 0

        while True:
            batch = await workflow.execute_activity(
                read_note_changes_batch,
                args=[lower_bound, cursor_change_at, cursor_id, params.batch_size],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry,
            )
            if not batch:
                break

            published = await workflow.execute_activity(
                publish_note_change_events,
                args=[batch],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry,
            )
            processed += published
            processed_this_run += published

            # Advance the keyset cursor + high-water mark to the last (sorted) row.
            cursor_change_at = batch[-1]["change_at"]
            cursor_id = batch[-1]["id"]
            last_change_at = cursor_change_at

            if len(batch) < params.batch_size:
                break

            if processed_this_run >= params.continue_as_new_threshold:
                workflow.continue_as_new(
                    args=[
                        ExportInput(
                            lower_bound=lower_bound,
                            cursor_change_at=cursor_change_at,
                            cursor_id=cursor_id,
                            processed=processed,
                            last_change_at=last_change_at,
                            batch_size=params.batch_size,
                            margin_seconds=params.margin_seconds,
                            continue_as_new_threshold=params.continue_as_new_threshold,
                        )
                    ]
                )

        # Persist progress only after the full delta is forwarded.
        if last_change_at is not None:
            await workflow.execute_activity(
                advance_export_watermark,
                args=[WATERMARK_NAME, last_change_at],
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=retry,
            )

        return processed


# Auto-discovery exports (read by the workflows package loader).
WORKFLOWS = [ExportNoteChangesWorkflow]
ACTIVITIES = [
    read_export_lower_bound,
    read_note_changes_batch,
    publish_note_change_events,
    advance_export_watermark,
]
