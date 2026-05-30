# LIB-EVENTS — projects/lakehouse/events (domain event envelope + publish)

**Unit:** LIB-EVENTS (Wavefront 2 library unit)
**ADR:** [agents/017 — Domain Event Schema and Tombstone Semantics](../../decisions/agents/017-domain-event-schema.md)
**Branch:** `feat/lakehouse-lib-events`
**Classification:** purely-additive — only new files under `projects/lakehouse/events/`.

## What shipped

`projects/lakehouse/events/`:

- `__init__.py` — public surface re-exporting the envelope, publish, and
  versioning helpers (`from projects.lakehouse.events import EventEnvelope, ...`).
- `envelope.py` — Pydantic v2 `EventEnvelope` with the exact ADR-017 field set
  (`schema_version=1` default, `entity_type`, `entity_id`, `event_type`,
  `event_version`, `event_id`, `occurred_at`, `producer`, `payload`,
  optional `correlation_id` / `caused_by`). `EventType` Literal of the five
  universal types. `new_event_id()` (inline UUIDv7), `nats_msg_id()`,
  `build_envelope()` convenience constructor.
- `publish.py` — `Publisher` `typing.Protocol` (DI seam, no nats_client import),
  `SUBJECT_BY_ENTITY` map, `subject_for()`, `async publish_event()`.
- `versioning.py` — `next_event_version(conn, entity_type, entity_id)` atomic
  upsert allocator + `CREATE_TABLE_SQL` for `lakehouse_event_versions`.
- `registry/__init__.py` — `pkgutil.iter_modules` auto-discovery loader exposing
  `SCHEMAS: dict[str, dict]` (+ `payload_model()` lookup helper).
- `registry/gap.py`, `registry/note.py` — Pydantic payload models with
  module-level `register(SCHEMAS)` hooks.
- Tests: `envelope_test.py`, `publish_test.py`, `versioning_test.py`,
  `registry/registry_test.py` (27 tests, all green locally).
- `BUILD` (events) + `registry/BUILD` — best-effort; gazelle (ci-format-bot)
  normalizes and adds `semgrep_test` targets on the PR branch.

## Key decisions

- **DI publish design.** `publish.py` defines a structural `Publisher` Protocol
  (`@runtime_checkable`) and never imports `projects.lakehouse.nats_client`. The
  parallel LIB-NATS JetStream wrapper satisfies it by duck typing, so the two
  units have **zero import edge** in either direction and ship independently.
  `publish_event` serializes via `model_dump_json()`, derives the subject
  (explicit arg → `subject_for`), and passes `{entity_id}-v{version}` as both
  `msg_id` and the `Nats-Msg-Id` header (ADR-017 idempotency layer 1).
- **UUIDv7 impl (no new dep).** `new_event_id()` builds an RFC-9562 UUIDv7 from a
  48-bit Unix-ms timestamp prefix + `os.urandom(10)`, sets the version nibble
  (`0b0111`) and variant bits (`0b10`), and formats the hyphenated 36-char hex.
  Time-prefixed → k-sortable / roughly time-ordered for trace correlation;
  strict per-entity ordering comes from `event_version`, not the id. Python
  3.11's `uuid` has no `uuid7`, so the inline impl avoids depending on stdlib
  version or a new pip dep.
- **Versioning via single atomic upsert.** `INSERT ... ON CONFLICT ... DO UPDATE
SET version = version + 1 RETURNING version` is race-free on both Postgres and
  sqlite ≥ 3.35 (tests use in-memory sqlite). `next_event_version` does **not**
  commit — it joins the producer's event-generation transaction. The
  `lakehouse_event_versions` table is created by a **deferred migration** (not
  this unit); `CREATE_TABLE_SQL` is exported for that migration and for tests.
- **Registry auto-discovery.** New event families = drop a module into
  `registry/` with a `register(SCHEMAS)` hook; no central list to edit. The
  loader skips `_*` and `*_test` modules so the test module in `registry/`
  (globbed into the test target's runfiles) never registers a spurious entity.

## Deviations from ADR 017

- **`event_type` is `str`, not the `EventType` Literal, on the model.** ADR 017
  explicitly allows domain-specific event types (e.g. gap-only `escalated`)
  alongside the five universal ones. Typing the field as `str` lets those
  validate; `EventType` is exported separately for the common typed path.
- **`extra="forbid"` on the envelope, `extra="allow"` on payloads.** The
  envelope rejects unknown top-level fields (typos fail fast); payloads tolerate
  extra keys per the ADR's additive schema-evolution rule.
- No other deviations. Field set matches the ADR-017 table exactly.

## Deferred / out of scope

- The `lakehouse_event_versions` migration (table DDL ships here as
  `CREATE_TABLE_SQL`; the actual migration is a later unit).
- Concrete NATS publishing (LIB-NATS unit provides the `Publisher` impl).
- `edge` payload registry module — `edge` has a subject mapping but no payload
  schema yet (no concrete use case; ADR open question #2 on cross-entity events).
- Embedding-length enforcement on `NoteChunk.embedding` (documented as 1024-dim
  voyage-4-nano, not validated, to tolerate a future model swap without a
  schema_version bump).

## Verification

Ran `PYTHONPATH=. python3 -m pytest projects/lakehouse/events/` locally
(system python3 + pydantic 2.12): **27 passed**. Imports clean; registry
auto-discovers `gap` + `note`. Authoritative test run is CI (`bazel test`).
