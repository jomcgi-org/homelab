# DOCS-EVENT-BUS — Status

**Unit:** DOCS-EVENT-BUS · **Status:** complete · **Classification:** [docs / purely-additive]
**Date:** 2026-05-30 · **Branch:** `feat/lakehouse-docs-event-bus`

## What shipped

One new operator-facing reference, `docs/event-bus.md`, derived from ADRs
`agents/015`, `agents/016`, `agents/017`. No existing file was modified.

### `docs/event-bus.md` section outline

1. **Purpose & scope** — NATS JetStream as the canonical event substrate; what
   belongs on the bus vs. what stays out (activity outputs, intra-activity calls,
   single-txn DB writes, request/response reads).
2. **Subject taxonomy** — `events.{domain}.{type}` table for the four domains
   (`knowledge`, `serving`, `ingest`, `ops`) plus the per-domain
   `events.<domain>.>` → one JetStream stream mapping deployed by
   `INFRA-NATS-STREAMS`.
3. **Event envelope schema** — JSON example + full field table (required vs.
   optional), with the ordering note that ranking is by `event_version`, not
   `occurred_at`.
4. **`Nats-Msg-Id` convention** — `{entity_id}-v{event_version}` and the three
   stacked idempotency layers (NATS dedup → deterministic Temporal workflow IDs →
   consumer `WHERE version < EXCLUDED.version`).
5. **Entity lifecycle & tombstones** — created → updated/processed → tombstoned
   state machine; tombstones as first-class events; physical purge at Iceberg
   compaction.
6. **Schema evolution rules** — additive-only; payload additions don't bump
   `schema_version`; removals/semantics-changes do.
7. **Producer / consumer quick-reference** — publish (subject + `Nats-Msg-Id`),
   subscribe + filter by `event_type`; pointer that dispatchers turn events into
   Temporal workflows (ADR 015).
8. **Security** — internal-only, KG sensitivity tier, per-subject NATS ACLs,
   Linkerd mTLS (no NetworkPolicies), reference-by-ID for large/sensitive payloads.

Cross-links to ADRs 015/016/017 and platform/004 at the top and in a See-also
footer.

## ADR ambiguities surfaced

- **Stream names are not named in the ADRs.** ADR 016 specifies the
  `events.<domain>.>` → one-stream-per-domain shape but does not give the stream
  identifiers. The doc uses `events-knowledge` / `events-serving` /
  `events-ingest` / `events-ops` as the natural convention and attributes them to
  `INFRA-NATS-STREAMS`; if that unit lands on different names, this table should be
  reconciled.
- **`failed` event type appears in the envelope table but not the lifecycle state
  machine.** ADR 017's field table lists `failed` as a valid `event_type`, and the
  per-consumer diagram shows a UI consumer filtering on `failed`, but the lifecycle
  state diagram omits a `failed` state. The doc documents `failed` as a valid
  `event_type` value without inventing a state-machine transition for it — worth a
  one-line ADR clarification on where `failed` sits in the lifecycle.
- **Cross-entity events** (e.g. an edge between two entities) are an open question
  in ADR 017 (single `entity_id` assumption). `events.knowledge.edge` is a live
  subject, so the first concrete edge event will force the
  "primary entity vs. `related_entities` array" decision the ADR defers.
