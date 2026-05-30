"""Unit tests for the domain event envelope (ADR agents/017)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from projects.lakehouse.events.envelope import (
    EventEnvelope,
    build_envelope,
    nats_msg_id,
    new_event_id,
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _sample() -> EventEnvelope:
    return build_envelope(
        entity_type="gap",
        entity_id="gap-42",
        event_type="created",
        event_version=1,
        producer="monolith.gardener",
        payload={"topic": "x", "context": {"k": "v"}},
    )


def test_build_envelope_fills_defaults():
    env = _sample()
    assert env.schema_version == 1
    assert env.event_id  # auto-filled UUIDv7
    assert _UUID_RE.match(env.event_id)
    assert env.occurred_at.tzinfo is not None  # tz-aware UTC default
    assert env.correlation_id is None
    assert env.caused_by is None


def test_envelope_round_trips_json():
    env = _sample()
    raw = env.model_dump_json()
    decoded = json.loads(raw)
    # Every ADR-017 field is present in the serialized form.
    assert set(decoded) == {
        "schema_version",
        "entity_type",
        "entity_id",
        "event_type",
        "event_version",
        "event_id",
        "occurred_at",
        "producer",
        "payload",
        "correlation_id",
        "caused_by",
    }
    rebuilt = EventEnvelope.model_validate_json(raw)
    assert rebuilt == env


def test_envelope_forbids_unknown_fields():
    with pytest.raises(Exception):
        EventEnvelope(
            entity_type="gap",
            entity_id="gap-1",
            event_type="created",
            event_version=1,
            event_id=new_event_id(),
            occurred_at=datetime.now(timezone.utc),
            producer="p",
            payload={},
            bogus_field="nope",
        )


def test_explicit_event_id_and_occurred_at_preserved():
    eid = new_event_id()
    ts = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    env = build_envelope(
        entity_type="note",
        entity_id="note-7",
        event_type="updated",
        event_version=3,
        producer="lakehouse.backfill",
        payload={},
        event_id=eid,
        occurred_at=ts,
        correlation_id="trace-abc",
        caused_by="evt-upstream",
    )
    assert env.event_id == eid
    assert env.occurred_at == ts
    assert env.correlation_id == "trace-abc"
    assert env.caused_by == "evt-upstream"


def test_nats_msg_id_format():
    assert nats_msg_id("gap-42", 1) == "gap-42-v1"
    assert nats_msg_id("note-7", 12) == "note-7-v12"


def test_new_event_id_is_valid_uuid_shape():
    eid = new_event_id()
    assert _UUID_RE.match(eid)
    # Version nibble must be 7 (UUIDv7): char at index 14 of the hyphenated form.
    assert eid[14] == "7"
    # Variant: first hex digit of the 4th group is 8, 9, a, or b.
    assert eid[19] in "89ab"


def test_new_event_id_unique():
    ids = {new_event_id() for _ in range(5000)}
    assert len(ids) == 5000


def test_new_event_id_roughly_time_ordered():
    # IDs minted later have a >= timestamp prefix (k-sortable). Compare the
    # 48-bit ms prefix across two batches separated by a tiny real delay.
    import time

    first = new_event_id()
    time.sleep(0.01)
    later = new_event_id()
    first_prefix = first.replace("-", "")[:12]
    later_prefix = later.replace("-", "")[:12]
    assert later_prefix >= first_prefix
