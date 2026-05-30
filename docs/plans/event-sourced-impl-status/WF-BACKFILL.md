# WF-BACKFILL — BackfillFromProcessedNotesWorkflow

**Unit:** WF-BACKFILL (part of Wavefront-3 WF-DOMAIN) · **Status:** complete
**Branch:** `feat/lakehouse-wf-domain` · **Author:** lakehouse WF-DOMAIN runner

The workflow that proves the event-sourced lakehouse pipeline end-to-end
(Wavefront-3 success criterion 3): it replays the bounded `_processed` KG corpus
(~4585 live notes, WAVEFRONT-0-discover §4) as `note` `created` domain events onto
`events.knowledge.note`.

## Files (all new, conflict-free per W3-PREP)

- `projects/lakehouse/orchestrator/workflows/backfill_processed_notes.py`
- `projects/lakehouse/orchestrator/workflows/backfill_processed_notes_test.py`

No BUILD file touched — the gazelle-excluded glob `workflows/BUILD` picks the new
files up and its `_WORKFLOW_DEPS` superset already covers `@pip//temporalio`,
`@pip//psycopg`, `@pip//psycopg_binary`, `@pip//pydantic`, and the
events/nats_client/orchestrator libraries.

## Batching & idempotency design

**Two activities, all I/O outside the deterministic workflow body:**

- `read_processed_notes_batch(offset, limit)` — read-only **raw SQL via psycopg**
  (no monolith model import; keeps the worker image off the `monolith_backend`
  dep graph). Reads `DATABASE_URL` (monolith-pg creds injected into the worker
  pod), normalizing the SQLAlchemy `postgresql+psycopg://` prefix to plain libpq.
  Two-step read: page `knowledge.notes` by `id` where `path LIKE '_processed/%'
AND deleted_at IS NULL` with `LIMIT/OFFSET`, then fetch all chunks for that page
  in one `WHERE note_fk = ANY(...)` query (`chunk_index`, `section_header`,
  `chunk_text`, `embedding::float[]` — the cast makes psycopg return a plain
  `list[float]` rather than a pgvector string). Returns plain JSON-serializable
  dicts. Paging the single notes table (not a row-multiplying JOIN) keeps
  `LIMIT/OFFSET` unambiguous.
- `publish_note_events(batch)` — for each note builds a validated
  `NoteCreatedPayload` (embeddings included) + ADR-017 `EventEnvelope`
  (`entity_type="note"`, `event_type="created"`, **`event_version=1`**,
  `entity_id=note_id`, `producer="lakehouse.backfill"`) and publishes via
  `NatsClient` + `events.publish_event`. The publish helper sets
  **`Nats-Msg-Id = {note_id}-v1`** as both `msg_id` and the `Nats-Msg-Id` header.

**Idempotency / re-runnability:** because every event's dedup key is the stable
`{note_id}-v1`, re-running the whole backfill republishes byte-identical msg-ids
and JetStream silently drops the duplicates (ADR 017 idempotency layer 1). The
workflow can be killed and restarted with no double-effect at any consumer. A
test asserts the msg-id is identical across two independent publish runs.

**Embeddings in the payload:** pre-computed voyage-4-nano (1024-dim) vectors ride
in `NoteChunk.embedding` so serving rebuilds never re-embed (platform/004 §"What
does NOT change"). A test exercises the full 1024-element vector through read →
payload.

**Resumability / history bloat:** the workflow paginates by `offset`, accumulating
a running total. It stops on a short page (fewer rows than requested = corpus
exhausted) and `continue_as_new`s once it has processed
`DEFAULT_CONTINUE_AS_NEW_THRESHOLD` (500) notes within a single run segment,
carrying `offset` + the running `processed` total into a fresh execution. This
keeps each run's Temporal event history small over the full ~4585-note corpus
(~92 batches at the default size of 50), so replay stays cheap (ADR 015 §worker
pod lifecycle / durability; risk table "workflow history bloat"). The workflow ID
is set by the caller (deterministic, e.g. `backfill-processed-notes`) so a
re-trigger dedups via Temporal's `WorkflowAlreadyStartedError` semantics.

## Tests (hermetic — no Temporal server, no DB, no network)

`asyncio.run`-driven (no pytest-asyncio plugin), matching the W2 test idiom.
psycopg's `connect` is patched to a fake connection returning canned `_processed`
rows (including a real 1024-float embedding); `NatsClient` is faked to capture
publishes. Coverage: read assembles correct dicts (incl. NULL visibility → "",
no-chunk notes, empty page, cross-note chunk separation); publish builds the right
envelope + `note_id-v1` msg-id; re-run produces an identical msg-id (idempotent);
empty batch is a no-op that never connects; the client is closed even on publish
error; the workflow is `@workflow.defn` and exported in `WORKFLOWS`; activities are
`@activity.defn` and exported in `ACTIVITIES`.

## Deviations / notes

- Two-step read instead of the spec's single LEFT JOIN, to keep `LIMIT/OFFSET`
  page boundaries unambiguous (a JOIN multiplies rows per note, so the page size
  would not map cleanly to a note count). Same data, same read-only guarantee.
- `DATABASE_URL` dialect normalization (`postgresql+psycopg://` → `postgresql://`)
  added so the worker can reuse the monolith's CNPG connection string verbatim.
