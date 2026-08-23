# Event Bus Reference

This document is the operator-facing reference for the homelab's NATS event bus:
the subject taxonomy, the domain event envelope schema, the idempotency
conventions, and the producer/consumer contract. It is derived from three ADRs —
read those for the _why_; read this for the _how_ when wiring a new producer or
consumer.

- [ADR 016 — NATS as the Canonical Event Stream](../decisions/agents/016-nats-canonical-event-stream.md) — subject topology, ownership, what stays out of NATS
- [ADR 017 — Domain Event Schema and Tombstone Semantics](../decisions/agents/017-domain-event-schema.md) — envelope fields, tombstones, versioning, schema evolution
- [ADR 015 — Temporal as the Orchestration Substrate](../decisions/agents/015-temporal-orchestration-substrate.md) — how workflow dispatchers consume events

## Purpose & scope

**NATS JetStream is the canonical event substrate for the homelab.** Every state
change that crosses a component boundary becomes a NATS event. Producers publish
to a subject with no knowledge of consumers; consumers subscribe to subjects with
no knowledge of producers. Temporal workflows are one consumer-type among many —
a producer never references Temporal directly.

The rule of thumb: **NATS is for events that should fan out to multiple potential
consumers, even if the second consumer doesn't exist yet.** It is _not_ a
general-purpose data mover.

### What belongs on the bus

- Cross-component state changes (a gap was discovered, a note was processed, an
  artifact is ready, an email arrived).
- Anything a future, not-yet-built consumer (UI notifications, metrics, analytics)
  might reasonably want to react to.

### What stays out of NATS

Do **not** NATS-ify these — they belong elsewhere (per ADR 016 §"What stays out of NATS"):

| Concern                            | Where it lives instead                                          |
| ---------------------------------- | --------------------------------------------------------------- |
| Activity outputs within a workflow | Temporal workflow state — accessed in-workflow, not republished |
| Sub-calls within an activity       | Direct function / SDK calls                                     |
| State updates in a single DB txn   | Postgres transaction — don't event-ify intra-transaction writes |
| Request/response reads (web app)   | HTTP endpoints against the monolith API                         |

## Subject taxonomy

Subjects follow `events.{domain}.{type}`. Domains namespace the event taxonomy;
each domain owns its event types, so adding a type in one domain never forces a
change on another domain's consumers.

| Subject                         | Domain      | Emits                                            |
| ------------------------------- | ----------- | ------------------------------------------------ |
| `events.knowledge.gap`          | `knowledge` | Knowledge-graph gap lifecycle                    |
| `events.knowledge.note`         | `knowledge` | Note lifecycle                                   |
| `events.knowledge.edge`         | `knowledge` | KG edge lifecycle                                |
| `events.serving.artifact-ready` | `serving`   | A serving artifact (e.g. Quack dataset) is ready |
| `events.ingest.email-arrived`   | `ingest`    | An inbound email landed                          |
| `events.ingest.calendar-event`  | `ingest`    | A calendar change was observed                   |
| `events.ops.alert-fired`        | `ops`       | An alert fired                                   |
| `events.ops.build-completed`    | `ops`       | A CI/build run completed                         |

### Stream mapping

Each domain maps to **one JetStream stream** capturing the wildcard
`events.<domain>.>`, as deployed by the `INFRA-NATS-STREAMS` unit:

| Stream             | Subject filter       |
| ------------------ | -------------------- |
| `events-knowledge` | `events.knowledge.>` |
| `events-serving`   | `events.serving.>`   |
| `events-ingest`    | `events.ingest.>`    |
| `events-ops`       | `events.ops.>`       |

One stream per domain means retention, replication, and consumer-group lag are
tuned per domain. NATS preserves ordering **within a subject**; cross-subject
ordering is not guaranteed (see ADR 016 Open Question 4 — for causal ordering
across subjects use `event_version` and/or `caused_by`, below).

## Event envelope schema

Every event — including deletions — is a versioned JSON document with a uniform
envelope (per ADR 017). Consumers interpret events by `event_type`; producers
never mutate state implicitly.

```json
{
  "schema_version": 1,
  "entity_type": "gap",
  "entity_id": "gap-42",
  "event_type": "created",
  "event_version": 1,
  "event_id": "evt-7f3a...",
  "occurred_at": "2026-05-30T12:00:00Z",
  "producer": "monolith.gardener",
  "payload": { "topic": "...", "context": {} },
  "correlation_id": "trace-abc...",
  "caused_by": "evt-1d20..."
}
```

| Field            | Type      | Required | Purpose                                                                                                |
| ---------------- | --------- | -------- | ------------------------------------------------------------------------------------------------------ |
| `schema_version` | int       | yes      | Envelope schema version (currently `1`). Coarse-grained; bumps only on breaking envelope change.       |
| `entity_type`    | string    | yes      | Domain entity class — `gap`, `note`, `edge`, etc.                                                      |
| `entity_id`      | string    | yes      | Stable identifier for the entity instance.                                                             |
| `event_type`     | string    | yes      | `created` \| `updated` \| `processed` \| `failed` \| `tombstoned` \| domain-specific.                  |
| `event_version`  | int       | yes      | Monotonic per `entity_id`; the producer's responsibility (Postgres sequence or entity version column). |
| `event_id`       | string    | yes      | Globally unique (UUIDv7 / ULID) for trace correlation.                                                 |
| `occurred_at`    | timestamp | yes      | Producer wall clock. Informational — **not** used for ordering.                                        |
| `producer`       | string    | yes      | Publishing component identifier, e.g. `monolith.gardener`.                                             |
| `payload`        | object    | yes      | Event-type-specific data; shape depends on `entity_type` + `event_type`.                               |
| `correlation_id` | string    | no       | OTel trace ID, for span continuation across event boundaries.                                          |
| `caused_by`      | string    | no       | `event_id` of the upstream event that triggered this one (causal lineage).                             |

Ordering is by `event_version` per entity, never by `occurred_at` — producer
clocks may drift, so `occurred_at` is informational only.

## `Nats-Msg-Id` convention

Publish every event with the JetStream dedup header:

```
Nats-Msg-Id: {entity_id}-v{event_version}
```

e.g. `gap-42-v1`. This drives the first of **three stacked idempotency layers**;
the net of all three is at-least-once delivery + idempotent application =
exactly-once effect.

| Layer    | Mechanism                                                     | Catches                                                                 |
| -------- | ------------------------------------------------------------- | ----------------------------------------------------------------------- |
| NATS     | `Nats-Msg-Id` dedup window in JetStream                       | Duplicate **publishes** (producer retried)                              |
| Workflow | Deterministic Temporal workflow IDs (`gap-drain-{entity_id}`) | Duplicate workflow **starts** (`WorkflowAlreadyStartedError` swallowed) |
| Consumer | `WHERE version < EXCLUDED.version` on the read-model upsert   | Duplicate **applications** (re-delivery / replay)                       |

A single event can be published twice, delivered twice, and applied twice — only
one effect lands.

## Entity lifecycle & tombstones

The lifecycle state machine is uniform across all entity types. `created`,
`updated`, and `tombstoned` are universal; per-entity domain-specific types (e.g.
gap `escalated`) can be added on top.

```
[*] --> created (v1)
created --> updated (vN+1)        updated --> updated (vN+1)
created --> processed (vN+1)      updated --> processed (vN+1)
created --> tombstoned (vN+1)     updated --> tombstoned (vN+1)
                                  processed --> tombstoned (vN+1)
tombstoned --> [*]  (physical purge on next Iceberg compaction)
```

**Tombstones are first-class events**, not an out-of-band delete protocol.
Deleting an entity means publishing a `tombstoned` event at the next
`event_version`. Each consumer applies tombstone semantics in its own domain (UI
removes from display; Iceberg writer appends a tombstone row). Benefits over
implicit deletion:

- Producers publish one event; they don't need to know each consumer's deletion protocol.
- Replay reconstructs full history **including** the deletion.
- Audit is inherent — the event stream _is_ the deletion log.
- Adding a consumer is a new subscription with zero producer change.

The cost is disk: tombstones aren't free. Logical deletion is immediate; **physical
purge happens at the next Iceberg compaction** (monthly base-layer rewrite per
platform/004, harvested into [`projects/platform/ARCHITECTURE.md`](../../projects/platform/ARCHITECTURE.md)). Urgent
right-to-be-forgotten deletes can trigger ad-hoc compaction on demand. A tombstone
event must not itself contain the data being forgotten — reference the `entity_id`
plus a redacted reason; the original `created`/`updated` events are what get purged.

## Schema evolution rules

The envelope is versioned coarsely; per-event-type schemas are not versioned
individually. Rules (per ADR 017):

- **Additive only.** New event types can be added freely — consumers ignore unknown types.
- **New payload fields don't bump `schema_version`.** Consumers tolerate extra fields.
- **Field removal** requires a new `schema_version` and a migration plan.
- **Field semantics changes** (e.g. new units on an existing field) require a new `schema_version`.
- **Entity types are namespaced per domain**, so collision risk is low.

Bump `schema_version` only for a breaking envelope change (removal or semantics
shift). Consumers check `schema_version` and dispatch to the matching decoder.
`event_version` (per-entity, monotonic) is a separate axis from `schema_version`
(envelope shape) — don't conflate them.

## Producer / consumer quick-reference

### Producing

1. Compute the next `event_version` for the entity (Postgres sequence keyed by
   `entity_id`, or the entity's incremented version column — generated
   transactionally so monotonicity holds).
2. Build the envelope; set `producer` to your component identifier (`monolith.<x>`).
3. Publish to `events.{domain}.{type}` with header `Nats-Msg-Id: {entity_id}-v{event_version}`.
4. Set `correlation_id` from the current OTel trace and `caused_by` from the
   triggering event's `event_id` when there is causal lineage. Both are optional
   but cheap and valuable.

### Consuming

1. Subscribe to the relevant subject(s) via a durable pull consumer (consumer group).
2. Filter by `event_type` for the slice you care about (e.g. a dispatcher takes
   only `created`; an Iceberg writer takes all).
3. Apply to your read model with the idempotency predicate
   `WHERE version < EXCLUDED.version`, then `Ack`.
4. Ignore unknown `event_type`s and tolerate unknown payload fields — that's what
   keeps additive evolution non-breaking.

**Events → workflows.** Workflow dispatchers are small (~30-line) adapters that
translate a NATS event into a `start_workflow` call with a deterministic workflow
ID. The producer stays decoupled from Temporal: it publishes an event, the
dispatcher reacts. Workflow outputs publish _back_ to NATS, closing the loop for
downstream consumers. See [ADR 015](../decisions/agents/015-temporal-orchestration-substrate.md)
for the dispatch/identity model and the cron-sweep that backstops missed events.

## Security

- **Internal-only.** No external client publishes or subscribes; the NATS server
  is reachable only from cluster pods. No new ingress is introduced by the bus.
- **Same sensitivity tier as the KG.** Events may reference KG content (note IDs,
  gap topics). Treat the streams as the same sensitivity tier as the knowledge
  graph itself — no external replication unless explicitly designed for it.
- **Per-subject authorization** via the NATS account model — producers and
  consumers get accounts scoped to exactly the subjects they need. Credentials are
  injected via the 1Password Operator at deploy time.
- **Cilium-meshed** like all internal traffic (Linkerd is superseded by
  Cilium). Any policy for the NATS namespace is expressed as a
  `CiliumNetworkPolicy`, not a plain NetworkPolicy.
- **Reference-by-ID for large or sensitive payloads.** Keep raw note bodies,
  embeddings, and other large/sensitive content out of the event; carry an ID (and
  an object-store URL if needed) instead. This stays under NATS's 1 MiB message
  limit and keeps sensitive bytes out of the durable audit log. Producers are
  responsible for not embedding PII that doesn't belong in an audit trail.

## See also

- [ADR 015 — Temporal as the Orchestration Substrate](../decisions/agents/015-temporal-orchestration-substrate.md)
- [ADR 016 — NATS as the Canonical Event Stream](../decisions/agents/016-nats-canonical-event-stream.md)
- [ADR 017 — Domain Event Schema and Tombstone Semantics](../decisions/agents/017-domain-event-schema.md)
- platform/004 Iceberg Lakehouse + Hot-Swap Quack Serving (superseded; see [`projects/platform/ARCHITECTURE.md`](../../projects/platform/ARCHITECTURE.md))
