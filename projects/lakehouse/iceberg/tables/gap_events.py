"""``gap_events`` Iceberg table: gap domain events (ADR agents/017).

Envelope columns mirror the ADR agents/017 event envelope; payload columns carry
the gap-specific fields (``topic``, ``gap_class``, ``state``). Gaps have no
embeddings, so there is exactly one row per gap event.
"""

from __future__ import annotations

from pyiceberg.schema import Schema
from pyiceberg.types import (
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

TABLE_NAME = "gap_events"

SCHEMA = Schema(
    # --- envelope (ADR agents/017) ---
    NestedField(1, "schema_version", IntegerType(), required=True),
    NestedField(2, "entity_type", StringType(), required=True),
    NestedField(3, "entity_id", StringType(), required=True),
    NestedField(4, "event_type", StringType(), required=True),
    NestedField(5, "event_version", LongType(), required=True),
    NestedField(6, "event_id", StringType(), required=True),
    NestedField(7, "occurred_at", TimestamptzType(), required=True),
    NestedField(8, "producer", StringType(), required=True),
    NestedField(9, "correlation_id", StringType(), required=False),
    NestedField(10, "caused_by", StringType(), required=False),
    # --- gap payload ---
    NestedField(11, "topic", StringType(), required=False),
    NestedField(12, "gap_class", StringType(), required=False),
    NestedField(13, "state", StringType(), required=False),
)
