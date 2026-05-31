"""Tests for the pkgutil-discovered TABLES registry and per-domain schemas."""

from __future__ import annotations

import pytest
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


@pytest.mark.parametrize(
    ("table_name", "payload_fields"),
    [
        ("note_events", NOTE_PAYLOAD_FIELDS),
        ("gap_events", GAP_PAYLOAD_FIELDS),
    ],
)
def test_table_has_envelope_and_payload(table_name, payload_fields):
    """Each domain table carries the full envelope + its payload columns."""
    names = _field_names(TABLES[table_name])
    assert ENVELOPE_FIELDS <= names
    assert payload_fields <= names


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


# --- TABLE_NAME constants ------------------------------------------------


def test_table_names_match_registry_keys():
    """The TABLE_NAME string in each module must equal its key in TABLES."""
    from projects.lakehouse.iceberg.tables import gap_events, note_events

    assert gap_events.TABLE_NAME == "gap_events"
    assert note_events.TABLE_NAME == "note_events"
    assert gap_events.TABLE_NAME in TABLES
    assert note_events.TABLE_NAME in TABLES


# --- per-table field required/optional flags ----------------------------


def test_note_events_note_id_is_required():
    """``note_id`` is required=True in note_events (it's the note's own PK
    column, distinct from the envelope's ``entity_id``)."""
    by_name = {f.name: f for f in TABLES["note_events"].fields}
    assert by_name["note_id"].required is True


def test_gap_events_payload_fields_are_optional():
    """Gap payload columns (topic, gap_class, state) are optional — a tombstone
    row for a gap carries no payload, so they must allow NULL."""
    by_name = {f.name: f for f in TABLES["gap_events"].fields}
    for col in ("topic", "gap_class", "state"):
        assert by_name[col].required is False, f"{col} should be optional"


def test_note_events_chunk_fields_are_optional():
    """Per-chunk columns are optional — a metadata-only note event (no chunks)
    produces a single row with these columns null."""
    by_name = {f.name: f for f in TABLES["note_events"].fields}
    for col in ("chunk_text", "chunk_index", "section_header"):
        assert by_name[col].required is False, f"{col} should be optional"


# --- list element IDs uniqueness -----------------------------------------


def test_note_events_list_element_ids_do_not_clash_with_field_ids():
    """The list element IDs (101, 102, 103) must be distinct from every
    top-level field ID in the schema (1–23)."""
    from pyiceberg.types import ListType

    schema = TABLES["note_events"]
    top_ids = {f.field_id for f in schema.fields}
    for f in schema.fields:
        if isinstance(f.field_type, ListType):
            elem_id = f.field_type.element_id
            assert elem_id not in top_ids, (
                f"List element ID {elem_id} on field '{f.name}' clashes with a "
                f"top-level field ID"
            )
