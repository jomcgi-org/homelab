"""Tests for the pkgutil-discovered TABLES registry and per-domain schemas."""

from __future__ import annotations

from pyiceberg.schema import Schema

from projects.lakehouse.iceberg.tables import TABLES

# ADR agents/017 envelope fields every domain table must carry.
ENVELOPE_FIELDS = {
    "schema_version",
    "entity_type",
    "entity_id",
    "event_type",
    "event_version",
    "event_id",
    "occurred_at",
    "producer",
    "correlation_id",
    "caused_by",
}

NOTE_PAYLOAD_FIELDS = {
    "note_id",
    "path",
    "title",
    "content_hash",
    "type",
    "status",
    "visibility",
    "tags",
    "aliases",
    "embedding",
    "chunk_text",
    "chunk_index",
    "section_header",
}

GAP_PAYLOAD_FIELDS = {"topic", "gap_class", "state"}


def _field_names(schema: Schema) -> set[str]:
    return {f.name for f in schema.fields}


def test_tables_discovers_both_domains():
    """pkgutil discovery registers note_events and gap_events."""
    assert "note_events" in TABLES
    assert "gap_events" in TABLES


def test_tables_values_are_schemas():
    """Every registered value is a pyiceberg Schema."""
    assert TABLES, "TABLES registry is empty"
    for name, schema in TABLES.items():
        assert isinstance(schema, Schema), f"{name} is not a Schema"


def test_note_events_has_envelope_and_payload():
    """note_events carries the full envelope + note payload columns."""
    names = _field_names(TABLES["note_events"])
    assert ENVELOPE_FIELDS <= names
    assert NOTE_PAYLOAD_FIELDS <= names


def test_gap_events_has_envelope_and_payload():
    """gap_events carries the full envelope + gap payload columns."""
    names = _field_names(TABLES["gap_events"])
    assert ENVELOPE_FIELDS <= names
    assert GAP_PAYLOAD_FIELDS <= names


def test_envelope_required_flags():
    """Required envelope fields are required; optional ones (correlation_id,
    caused_by) are optional, per ADR agents/017."""
    schema = TABLES["gap_events"]
    by_name = {f.name: f for f in schema.fields}
    for required in ENVELOPE_FIELDS - {"correlation_id", "caused_by"}:
        assert by_name[required].required, f"{required} should be required"
    assert not by_name["correlation_id"].required
    assert not by_name["caused_by"].required


def test_note_embedding_is_a_list_column():
    """The note embedding is modeled as a list<float> column (one row/chunk)."""
    from pyiceberg.types import ListType

    by_name = {f.name: f for f in TABLES["note_events"].fields}
    assert isinstance(by_name["embedding"].field_type, ListType)
    assert isinstance(by_name["tags"].field_type, ListType)
    assert isinstance(by_name["aliases"].field_type, ListType)


def test_unique_field_ids_per_schema():
    """Iceberg requires globally-unique field IDs within a schema (incl. list
    element IDs); a duplicate would make the schema invalid."""
    for name, schema in TABLES.items():
        ids = [f.field_id for f in schema.fields]
        assert len(ids) == len(set(ids)), f"{name} has duplicate top-level field IDs"
