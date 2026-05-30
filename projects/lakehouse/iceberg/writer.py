"""Iceberg writer helpers: event rows -> pyarrow -> table append (platform/004).

These are the low-level building blocks the Wavefront-3 ``IcebergBatchCommitWorkflow``
calls once per batch drained from NATS. They do not own batching, ordering, or
dedup — that is the workflow's job (see idempotency note on ``append_events``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pyarrow as pa
    from pyiceberg.schema import Schema
    from pyiceberg.table import Table


def rows_to_arrow(rows: list[dict[str, Any]], schema: "Schema") -> "pa.Table":
    """Convert a list of event-row dicts into a ``pyarrow.Table``.

    The arrow table is built against the pyarrow schema *derived from the
    iceberg* ``Schema`` (``pyiceberg.io.pyarrow.schema_to_pyarrow``) so column
    order, names, types, and field IDs line up with what ``table.append``
    expects. Each row dict is keyed by column name; missing optional columns
    come through as nulls.

    Args:
        rows: event rows, one dict per Iceberg row (column-name -> value).
        schema: the target table's pyiceberg ``Schema``.

    Returns:
        A ``pyarrow.Table`` whose schema matches ``schema``.
    """
    import pyarrow as pa
    from pyiceberg.io.pyarrow import schema_to_pyarrow

    arrow_schema = schema_to_pyarrow(schema)
    # pa.Table.from_pylist aligns each dict's keys to the provided schema's
    # field names, filling absent keys with null and ignoring extras.
    return pa.Table.from_pylist(rows, schema=arrow_schema)


def append_events(table: "Table", rows: list[dict[str, Any]]) -> None:
    """Append event rows to an Iceberg ``table`` (commits a new snapshot).

    Idempotency
    -----------
    This helper performs a **plain append** — it does no dedup of its own. The
    end-to-end pipeline is idempotent at three other layers (ADR agents/017):

      1. NATS-level: ``Nats-Msg-Id = {entity_id}-v{event_version}`` drops
         duplicate publishes inside JetStream's dedup window.
      2. Workflow-level: the Wavefront-3 ``IcebergBatchCommitWorkflow`` uses a
         deterministic batch identity and acks the drained batch only after the
         Iceberg commit succeeds, so a re-run of a *failed* commit re-drains the
         same messages rather than double-committing.
      3. Backfill: re-running the one-shot backfill (Wavefront 5) republishes the
         same ``created`` events with the same ``Nats-Msg-Id``, deduped at
         layer 1 before they ever reach this writer.

    At the Iceberg layer we therefore append unconditionally and rely on the
    batch-commit workflow for dedup. A naive double-call of this function WILL
    write duplicate rows — that contract is intentional and owned upstream.

    Args:
        table: the loaded pyiceberg ``Table`` to append to.
        rows: event rows to append (see ``rows_to_arrow``). No-op if empty.
    """
    if not rows:
        return
    table.append(rows_to_arrow(rows, table.schema()))
