"""Unit tests for the auto-discovering payload schema registry (ADR agents/017)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from projects.lakehouse.events import registry
from projects.lakehouse.events.registry import SCHEMAS, payload_model
from projects.lakehouse.events.registry.gap import GapCreatedPayload
from projects.lakehouse.events.registry.note import NoteChunk, NoteCreatedPayload


def test_loader_discovers_gap_and_note():
    # Both registry modules were auto-imported and registered their entities.
    assert "gap" in SCHEMAS
    assert "note" in SCHEMAS


def test_gap_schemas_populated():
    assert SCHEMAS["gap"]["created"] is GapCreatedPayload
    assert "updated" in SCHEMAS["gap"]
    assert "tombstoned" in SCHEMAS["gap"]


def test_note_schemas_populated():
    assert SCHEMAS["note"]["created"] is NoteCreatedPayload
    assert issubclass(SCHEMAS["note"]["created"], BaseModel)


def test_payload_model_lookup():
    assert payload_model("gap", "created") is GapCreatedPayload
    assert payload_model("note", "created") is NoteCreatedPayload


def test_payload_model_unknown_returns_none():
    assert payload_model("gap", "does-not-exist") is None
    assert payload_model("unknown-entity", "created") is None


def test_reload_is_idempotent():
    before = {et: dict(types) for et, types in SCHEMAS.items()}
    registry._load()
    after = {et: dict(types) for et, types in SCHEMAS.items()}
    assert before.keys() == after.keys()
    for et in before:
        assert before[et] == after[et]


def test_test_modules_not_registered_as_entities():
    # The loader skips ``*_test`` modules, so this test module's import never
    # pollutes SCHEMAS with a spurious entity type.
    assert "registry_test" not in SCHEMAS


def test_gap_created_payload_validates():
    p = GapCreatedPayload(topic="t", gap_class="external", state="new")
    assert p.topic == "t"
    assert p.gap_class == "external"
    assert p.state == "new"


def test_gap_created_payload_requires_topic():
    # ``topic`` is required; omitting it raises a pydantic ValidationError.
    with pytest.raises(ValidationError):
        GapCreatedPayload(gap_class="external", state="new")


def test_note_created_payload_with_chunks():
    chunk = NoteChunk(
        chunk_index=0,
        section_header="Intro",
        chunk_text="hello",
        embedding=[0.0] * 1024,
    )
    note = NoteCreatedPayload(
        note_id="n1",
        path="notes/n1.md",
        title="N1",
        content_hash="abc",
        type="reference",
        status="published",
        visibility="public",
        tags=["a", "b"],
        aliases=["alt"],
        chunks=[chunk],
    )
    assert len(note.chunks) == 1
    assert len(note.chunks[0].embedding) == 1024


# --- GapUpdatedPayload ---------------------------------------------------


def test_gap_updated_payload_requires_state():
    from projects.lakehouse.events.registry.gap import GapUpdatedPayload

    with pytest.raises(ValidationError):
        GapUpdatedPayload()  # state is required


def test_gap_updated_payload_optional_fields():
    from projects.lakehouse.events.registry.gap import GapUpdatedPayload

    # topic, gap_class, and context are optional.
    p = GapUpdatedPayload(state="answered")
    assert p.state == "answered"
    assert p.topic is None
    assert p.gap_class is None
    assert p.context is None


def test_gap_updated_payload_with_all_fields():
    from projects.lakehouse.events.registry.gap import GapUpdatedPayload

    p = GapUpdatedPayload(
        topic="epistemology",
        gap_class="external",
        state="researching",
        context={"source": "web"},
    )
    assert p.topic == "epistemology"
    assert p.state == "researching"
    assert p.context == {"source": "web"}


# --- GapTombstonedPayload ------------------------------------------------


def test_gap_tombstoned_payload_no_required_fields():
    from projects.lakehouse.events.registry.gap import GapTombstonedPayload

    # Tombstone has no required fields — entity_id lives in the envelope.
    p = GapTombstonedPayload()
    assert p.reason is None


def test_gap_tombstoned_payload_reason():
    from projects.lakehouse.events.registry.gap import GapTombstonedPayload

    p = GapTombstonedPayload(reason="duplicate")
    assert p.reason == "duplicate"


def test_gap_schemas_has_all_three_event_types():
    assert set(SCHEMAS["gap"]) >= {"created", "updated", "tombstoned"}


# --- NoteUpdatedPayload --------------------------------------------------


def test_note_updated_payload_is_subclass_of_created():
    from projects.lakehouse.events.registry.note import NoteUpdatedPayload

    assert issubclass(NoteUpdatedPayload, NoteCreatedPayload)


def test_note_updated_payload_validates():
    from projects.lakehouse.events.registry.note import NoteUpdatedPayload

    p = NoteUpdatedPayload(
        note_id="n2",
        path="notes/n2.md",
        title="N2",
        content_hash="def",
        type="permanent",
        status="draft",
        visibility="private",
    )
    assert p.note_id == "n2"
    assert p.tags == []


# --- NoteTombstonedPayload -----------------------------------------------


def test_note_tombstoned_payload_requires_note_id():
    from projects.lakehouse.events.registry.note import NoteTombstonedPayload

    with pytest.raises(ValidationError):
        NoteTombstonedPayload()  # note_id is required


def test_note_tombstoned_payload_reason_optional():
    from projects.lakehouse.events.registry.note import NoteTombstonedPayload

    p = NoteTombstonedPayload(note_id="n3")
    assert p.note_id == "n3"
    assert p.reason is None


def test_note_tombstoned_payload_with_reason():
    from projects.lakehouse.events.registry.note import NoteTombstonedPayload

    p = NoteTombstonedPayload(note_id="n4", reason="RTBF request")
    assert p.reason == "RTBF request"


def test_note_schemas_has_all_event_types():
    assert set(SCHEMAS["note"]) >= {"created", "updated", "tombstoned"}


# --- NoteChunk edge cases ------------------------------------------------


def test_note_chunk_section_header_optional():
    chunk = NoteChunk(chunk_index=1, chunk_text="body text", embedding=[0.1])
    assert chunk.section_header is None


def test_note_chunk_requires_chunk_text():
    with pytest.raises(ValidationError):
        NoteChunk(chunk_index=0, embedding=[0.0])  # chunk_text required


# --- NoteCreatedPayload defaults -----------------------------------------


def test_note_created_payload_defaults_to_empty_lists():
    p = NoteCreatedPayload(
        note_id="n5",
        path="notes/n5.md",
        title="N5",
        content_hash="ghi",
        type="fleeting",
        status="published",
        visibility="public",
    )
    assert p.tags == []
    assert p.aliases == []
    assert p.chunks == []


# --- EMBEDDING_DIM constant ----------------------------------------------


def test_embedding_dim_constant():
    from projects.lakehouse.events.registry.note import EMBEDDING_DIM

    assert EMBEDDING_DIM == 1024


# --- register() called directly ------------------------------------------


def test_gap_register_populates_fresh_dict():
    from projects.lakehouse.events.registry.gap import (
        GapCreatedPayload,
        GapTombstonedPayload,
        GapUpdatedPayload,
        register,
    )

    fresh: dict = {}
    register(fresh)
    assert fresh["gap"]["created"] is GapCreatedPayload
    assert fresh["gap"]["updated"] is GapUpdatedPayload
    assert fresh["gap"]["tombstoned"] is GapTombstonedPayload


def test_note_register_populates_fresh_dict():
    from projects.lakehouse.events.registry.note import (
        NoteCreatedPayload,
        NoteTombstonedPayload,
        NoteUpdatedPayload,
        register,
    )

    fresh: dict = {}
    register(fresh)
    assert fresh["note"]["created"] is NoteCreatedPayload
    assert fresh["note"]["updated"] is NoteUpdatedPayload
    assert fresh["note"]["tombstoned"] is NoteTombstonedPayload


def test_gap_register_is_idempotent():
    from projects.lakehouse.events.registry.gap import GapCreatedPayload, register

    d: dict = {}
    register(d)
    register(d)  # second call must not raise or duplicate keys
    assert d["gap"]["created"] is GapCreatedPayload
