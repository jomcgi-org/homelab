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
