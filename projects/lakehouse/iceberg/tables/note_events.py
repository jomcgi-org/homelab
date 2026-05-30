"""``note_events`` Iceberg table: note domain events + per-chunk embeddings.

Envelope columns mirror the ADR agents/017 event envelope; payload columns carry
the note metadata; the embedding columns carry one chunk's text + vector.

Embedding / chunk modeling choice
---------------------------------
**One row per chunk** (not a nested list of chunks per note event). Each row is a
full envelope + note-payload copy plus a single ``embedding`` vector and its
``chunk_text`` / ``chunk_index`` / ``section_header``. A note event that splits
into N chunks produces N rows sharing the same ``event_id`` / ``entity_id`` /
``event_version``.

Rationale (platform/004 §serving build): the serving artifact is a DuckDB+VSS
HNSW index over embeddings. A flat "one embedding per row" layout maps directly
to an HNSW build (``SELECT embedding, note_id, chunk_text ...``) with no
``UNNEST`` of a nested list, and lets vector search return the matching chunk and
its parent note in a single row. The cost is envelope duplication across a note's
chunks — acceptable at homelab note volume and well-compressed by Parquet's
dictionary/RLE encoding of the repeated envelope columns. A note event with no
chunks (e.g. a metadata-only ``updated`` or a ``tombstoned``) is a single row
with ``embedding`` / ``chunk_*`` null.

Embedding dimensionality: 1024-dim ``voyage-4-nano`` (``list<float>``). Per
platform/004 OQ#5, ``array<float>`` doesn't compress well; int8 quantization at
rest is a deferred follow-up, not modeled here.
"""

from __future__ import annotations

from pyiceberg.schema import Schema
from pyiceberg.types import (
    FloatType,
    IntegerType,
    ListType,
    LongType,
    NestedField,
    StringType,
    TimestamptzType,
)

TABLE_NAME = "note_events"

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
    # --- note payload ---
    NestedField(11, "note_id", StringType(), required=True),
    NestedField(12, "path", StringType(), required=False),
    NestedField(13, "title", StringType(), required=False),
    NestedField(14, "content_hash", StringType(), required=False),
    NestedField(15, "type", StringType(), required=False),
    NestedField(16, "status", StringType(), required=False),
    NestedField(17, "visibility", StringType(), required=False),
    NestedField(
        18,
        "tags",
        ListType(element_id=101, element=StringType(), element_required=False),
        required=False,
    ),
    NestedField(
        19,
        "aliases",
        ListType(element_id=102, element=StringType(), element_required=False),
        required=False,
    ),
    # --- per-chunk embedding (one row per chunk; see module docstring) ---
    NestedField(
        20,
        "embedding",
        ListType(element_id=103, element=FloatType(), element_required=False),
        required=False,
    ),
    NestedField(21, "chunk_text", StringType(), required=False),
    NestedField(22, "chunk_index", IntegerType(), required=False),
    NestedField(23, "section_header", StringType(), required=False),
)
