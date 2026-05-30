"""``IcebergBatchCommitWorkflow`` — drain NATS event subjects into Iceberg.

This is the lakehouse **write path** (ADR platform/004 §"Write path"): the
canonical event stream lives in NATS JetStream; this workflow batches events out
of NATS and commits them as Iceberg snapshots on SeaweedFS roughly every 1-2
minutes. The schedule that drives that cadence is defined by the sibling
WF-SCHEDULES unit — this module only defines the workflow + its activity.

At-least-once + idempotency contract
------------------------------------
Delivery is **at-least-once**. The activity acks NATS messages only **after** the
Iceberg ``append`` commit succeeds, so a crash between commit and ack re-delivers
the batch and re-appends it. That double-write is tolerated because dedup is
owned upstream (ADR agents/017, three layers):

  1. NATS publish dedup: producers set ``Nats-Msg-Id = {entity_id}-v{version}``
     so a re-publish of the same per-entity version is dropped inside JetStream's
     dedup window before it ever reaches this consumer.
  2. This workflow: it acks a drained batch only after a successful Iceberg
     commit. A failed commit leaves the messages un-acked; JetStream redelivers
     them to the next run, which re-commits rather than skipping data.
  3. Backfill (WF-BACKFILL): re-running the one-shot backfill republishes the
     same ``created`` events with the same ``Nats-Msg-Id``, deduped at layer 1.

The narrow residual window — commit succeeds, process dies before ack — produces
duplicate Iceberg rows on redelivery (``iceberg.writer.append_events`` does a
plain append by design). That is the deliberate at-least-once tradeoff; readers
fold to the latest ``event_version`` per ``entity_id``, so duplicate raw rows do
not corrupt derived state. Exactly-once would require a transactional
NATS-ack-with-Iceberg-commit that neither system offers; at-least-once + an
idempotent read fold is the simpler, correct choice for this pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta

import temporalio.activity
import temporalio.workflow
from temporalio.common import RetryPolicy

# Heavy I/O deps (nats, pyiceberg, pyarrow) must NOT be imported into the
# workflow sandbox. They are pulled in only inside the activity function body so
# Temporal's deterministic-import check never sees them.

# NATS subjects this workflow drains. Mirrors ``events.publish.SUBJECT_BY_ENTITY``
# but expressed as a single wildcard so one durable consumer covers every current
# and future ``events.knowledge.*`` entity type (note, gap, edge, ...).
DRAIN_SUBJECT = "events.knowledge.*"

# Durable pull-consumer name. Stable so the consumer's ack/redelivery state
# survives worker restarts (a fresh durable would re-read from the stream start).
DURABLE_CONSUMER = "iceberg-batch-commit"

# Iceberg namespace the ``*_events`` tables live in. ADR platform/004 names the
# warehouse hierarchy ``warehouse.knowledge.*``; "warehouse" is the catalog
# (``iceberg.catalog.CATALOG_NAME``) and "knowledge" is the namespace. The W2
# table modules (``iceberg.tables``) define only the leaf TABLE_NAME, leaving the
# namespace as the seam closed here. Overridable via ``ICEBERG_NAMESPACE`` env so
# table creation (WF-DOMAIN) and this drainer stay in sync from one knob.
ICEBERG_NAMESPACE = "knowledge"

# entity_type -> Iceberg table name. The actual schemas live in
# ``iceberg.tables.TABLES`` (keyed by table name); this maps the envelope's
# ``entity_type`` to the table it lands in. Unmapped entity types are skipped (and
# their messages NOT acked) so a new entity type can't be silently dropped.
TABLE_BY_ENTITY: dict[str, str] = {
    "note": "note_events",
    "gap": "gap_events",
}

# How many messages to pull per drain. One JetStream fetch per activity run; the
# workflow loops via continue_as_new for sustained draining.
DEFAULT_BATCH = 500

# Activity timeouts / retries. The drain touches NATS + S3, so give it room and
# retry transient failures; the at-least-once contract makes retry safe.
_ACTIVITY_TIMEOUT = timedelta(minutes=5)
_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


@dataclass
class DrainResult:
    """Outcome of one ``drain_and_commit`` run.

    ``committed_by_table`` maps Iceberg table name -> rows appended this run;
    ``acked`` is the number of NATS messages acked (== messages whose entity_type
    resolved to a table and committed successfully). ``empty`` is True when the
    fetch returned nothing (the workflow uses it to decide whether to keep
    looping aggressively or back off to the schedule).
    """

    acked: int = 0
    committed_by_table: dict[str, int] = field(default_factory=dict)
    skipped_unmapped: int = 0
    empty: bool = False


def _envelope_to_row(envelope: dict) -> dict:
    """Flatten an :class:`EventEnvelope` dict into an Iceberg row dict.

    Envelope fields map 1:1 to the envelope columns shared by every ``*_events``
    table; the ``payload`` dict's keys are spread into the payload columns. Keys
    the target table's schema doesn't declare are dropped by
    ``writer.rows_to_arrow`` (``pa.Table.from_pylist`` ignores extras), so a
    superset payload is safe. ``occurred_at`` is left as the envelope's ISO
    string; pyarrow casts it to the table's timestamptz column.
    """
    payload = envelope.get("payload") or {}
    row = {
        "schema_version": envelope.get("schema_version"),
        "entity_type": envelope.get("entity_type"),
        "entity_id": envelope.get("entity_id"),
        "event_type": envelope.get("event_type"),
        "event_version": envelope.get("event_version"),
        "event_id": envelope.get("event_id"),
        "occurred_at": envelope.get("occurred_at"),
        "producer": envelope.get("producer"),
        "correlation_id": envelope.get("correlation_id"),
        "caused_by": envelope.get("caused_by"),
    }
    # Spread payload columns (note_id, path, embedding, topic, gap_class, ...).
    # rows_to_arrow drops any key not in the target schema.
    row.update(payload)
    return row


@temporalio.activity.defn
async def drain_and_commit() -> DrainResult:
    """Pull one batch from NATS, group by table, commit to Iceberg, then ack.

    Steps (all I/O — this is why it's an activity, not workflow code):

      1. Connect a :class:`NatsClient` and open the durable pull consumer over
         ``events.knowledge.*``.
      2. Fetch up to :data:`DEFAULT_BATCH` messages; deserialize each into an
         :class:`EventEnvelope`.
      3. Group rows by target Iceberg table via :data:`TABLE_BY_ENTITY`.
         Unmapped entity types are skipped and their messages left un-acked.
      4. For each table: load it from ``load_warehouse_catalog()`` and
         ``append_events`` (one Iceberg commit per table).
      5. Ack the messages whose table committed successfully. Ack happens
         **only after** the commit — the at-least-once contract (module docstring).

    Returns a :class:`DrainResult` for observability; never raises on an empty
    fetch (returns ``empty=True``).
    """
    import nats.errors

    from projects.lakehouse.events.envelope import EventEnvelope
    from projects.lakehouse.iceberg.catalog import load_warehouse_catalog
    from projects.lakehouse.iceberg.writer import append_events
    from projects.lakehouse.nats_client.client import NatsClient

    namespace = os.environ.get("ICEBERG_NAMESPACE", ICEBERG_NAMESPACE)

    client = NatsClient()
    await client.connect()
    try:
        sub = await client.pull_subscribe(
            DRAIN_SUBJECT, durable=DURABLE_CONSUMER, batch=DEFAULT_BATCH
        )
        try:
            msgs = await sub.fetch(DEFAULT_BATCH, timeout=5.0)
        except nats.errors.TimeoutError:
            # No messages within the fetch window — a normal idle drain.
            return DrainResult(empty=True)

        if not msgs:
            return DrainResult(empty=True)

        # Group the batch by target table, keeping each row paired with its
        # message so we ack exactly the messages whose table committed.
        rows_by_table: dict[str, list[dict]] = {}
        msgs_by_table: dict[str, list] = {}
        skipped = 0
        for msg in msgs:
            # Validate once (surfaces malformed events loudly) then dump to a
            # JSON-mode dict so occurred_at is an ISO string and nested payload
            # types are Iceberg/pyarrow-friendly.
            envelope = EventEnvelope.model_validate_json(msg.data)
            table = TABLE_BY_ENTITY.get(envelope.entity_type)
            if table is None:
                # Unknown entity type: don't ack (so it isn't lost) — surfaces as
                # a redelivering message that ops can investigate.
                skipped += 1
                continue
            rows_by_table.setdefault(table, []).append(
                _envelope_to_row(envelope.model_dump(mode="json"))
            )
            msgs_by_table.setdefault(table, []).append(msg)

        catalog = load_warehouse_catalog()
        result = DrainResult(skipped_unmapped=skipped)
        for table_name, rows in rows_by_table.items():
            table = catalog.load_table((namespace, table_name))
            append_events(table, rows)  # Iceberg commit (new snapshot).
            # Commit succeeded for this table — now (and only now) ack its msgs.
            for msg in msgs_by_table[table_name]:
                await msg.ack()
            result.committed_by_table[table_name] = len(rows)
            result.acked += len(msgs_by_table[table_name])
        return result
    finally:
        await client.close()


@temporalio.workflow.defn
class IcebergBatchCommitWorkflow:
    """Drain NATS -> Iceberg, looping via continue_as_new (ADR platform/004).

    The WF-SCHEDULES unit starts this on a ~1-2min cadence. Within a run it drains
    repeatedly until a fetch comes back empty, then continues-as-new so history
    stays bounded and the next schedule tick (or this same loop) picks up where it
    left off. All I/O is in :func:`drain_and_commit`; the workflow body only
    sequences activity calls and decides when to loop — it stays deterministic
    (no ``datetime.now``, no NATS/S3 here).
    """

    @temporalio.workflow.run
    async def run(self, max_drains_per_run: int = 50) -> int:
        """Drain until empty (bounded by ``max_drains_per_run``); return rows acked.

        ``max_drains_per_run`` caps how many activity invocations happen before
        forcing a ``continue_as_new`` so a busy stream can't grow this run's
        history unboundedly.
        """
        total_acked = 0
        for _ in range(max_drains_per_run):
            result = await temporalio.workflow.execute_activity(
                drain_and_commit,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY_POLICY,
            )
            total_acked += result.acked
            if result.empty:
                # Stream drained — let the schedule trigger the next run.
                return total_acked
        # Hit the per-run drain cap with the stream still non-empty: loop without
        # growing this run's history.
        temporalio.workflow.continue_as_new(max_drains_per_run)


WORKFLOWS = [IcebergBatchCommitWorkflow]
ACTIVITIES = [drain_and_commit]
