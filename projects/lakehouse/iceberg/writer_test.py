"""Tests for rows_to_arrow — dict rows -> pyarrow.Table (no catalog/S3)."""

from __future__ import annotations

import datetime

import pyarrow as pa

from projects.lakehouse.iceberg.tables.note_events import SCHEMA as NOTE_SCHEMA
from projects.lakehouse.iceberg.writer import append_events, rows_to_arrow

# A timestamptz column expects a tz-aware datetime, not an ISO string —
# pyarrow.Table.from_pylist coerces datetime -> timestamp but not str.
_TS = datetime.datetime(2026, 5, 30, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _sample_rows() -> list[dict]:
    return [
        {
            "schema_version": 1,
            "entity_type": "note",
            "entity_id": "note-1",
            "event_type": "created",
            "event_version": 1,
            "event_id": "evt-1",
            "occurred_at": _TS,
            "producer": "monolith.gardener",
            "correlation_id": None,
            "caused_by": None,
            "note_id": "note-1",
            "path": "notes/note-1.md",
            "title": "First note",
            "content_hash": "abc123",
            "type": "permanent",
            "status": "active",
            "visibility": "public",
            "tags": ["a", "b"],
            "aliases": [],
            "embedding": [0.1, 0.2, 0.3],
            "chunk_text": "chunk zero",
            "chunk_index": 0,
            "section_header": "Intro",
        },
        {
            "schema_version": 1,
            "entity_type": "note",
            "entity_id": "note-1",
            "event_type": "created",
            "event_version": 1,
            "event_id": "evt-1",
            "occurred_at": _TS,
            "producer": "monolith.gardener",
            "correlation_id": None,
            "caused_by": None,
            "note_id": "note-1",
            "path": "notes/note-1.md",
            "title": "First note",
            "content_hash": "abc123",
            "type": "permanent",
            "status": "active",
            "visibility": "public",
            "tags": ["a", "b"],
            "aliases": [],
            "embedding": [0.4, 0.5, 0.6],
            "chunk_text": "chunk one",
            "chunk_index": 1,
            "section_header": "Body",
        },
    ]


def test_rows_to_arrow_columns_match_schema():
    """The arrow table has exactly the iceberg schema's columns, in order."""
    table = rows_to_arrow(_sample_rows(), NOTE_SCHEMA)
    expected = [f.name for f in NOTE_SCHEMA.fields]
    assert table.column_names == expected


def test_rows_to_arrow_row_count():
    """One arrow row per input dict (one row per chunk)."""
    table = rows_to_arrow(_sample_rows(), NOTE_SCHEMA)
    assert table.num_rows == 2


def test_rows_to_arrow_types():
    """Envelope + payload columns get the expected arrow types."""
    table = rows_to_arrow(_sample_rows(), NOTE_SCHEMA)
    schema = table.schema
    assert pa.types.is_integer(schema.field("schema_version").type)
    assert pa.types.is_integer(schema.field("event_version").type)
    # pyiceberg maps StringType -> arrow large_string (not string).
    assert pa.types.is_large_string(schema.field("entity_id").type)
    assert pa.types.is_timestamp(schema.field("occurred_at").type)
    # pyiceberg maps ListType -> arrow large_list (not list).
    assert pa.types.is_large_list(schema.field("tags").type)
    assert pa.types.is_large_list(schema.field("embedding").type)
    assert pa.types.is_floating(schema.field("embedding").type.value_type)


def test_rows_to_arrow_preserves_values():
    """Scalar and list values survive the round-trip into arrow."""
    table = rows_to_arrow(_sample_rows(), NOTE_SCHEMA)
    py = table.to_pylist()
    assert py[0]["entity_id"] == "note-1"
    assert py[0]["chunk_index"] == 0
    assert py[1]["chunk_index"] == 1
    assert py[0]["tags"] == ["a", "b"]
    assert py[1]["chunk_text"] == "chunk one"


def test_rows_to_arrow_empty():
    """Empty input yields a zero-row table with the right columns."""
    table = rows_to_arrow([], NOTE_SCHEMA)
    assert table.num_rows == 0
    assert table.column_names == [f.name for f in NOTE_SCHEMA.fields]


class _FakeTable:
    """Minimal stand-in for a pyiceberg Table (no catalog/S3 needed)."""

    def __init__(self, schema):
        self._schema = schema
        self.appended: list[pa.Table] = []

    def schema(self):
        return self._schema

    def append(self, arrow_table):
        self.appended.append(arrow_table)


def test_append_events_calls_table_append():
    """append_events converts rows and appends the arrow table."""
    fake = _FakeTable(NOTE_SCHEMA)
    append_events(fake, _sample_rows())
    assert len(fake.appended) == 1
    assert fake.appended[0].num_rows == 2


def test_append_events_noop_on_empty():
    """append_events does not call table.append for an empty batch."""
    fake = _FakeTable(NOTE_SCHEMA)
    append_events(fake, [])
    assert fake.appended == []
