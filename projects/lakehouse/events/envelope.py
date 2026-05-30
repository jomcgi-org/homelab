"""The canonical domain event envelope (ADR agents/017).

A single system-wide envelope wraps every cross-component state change. The
fields here mirror the ADR-017 spec exactly; see that ADR for the rationale
behind each field, the entity lifecycle state machine, and tombstone semantics.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Universal event types per ADR 017. Entity-specific types (e.g. a gap-only
# ``escalated``) are allowed by the spec but the envelope keeps the universal
# set as a Literal for type-checking the common path. ``event_type`` on the
# model is a plain ``str`` so domain-specific values still validate.
EventType = Literal["created", "updated", "processed", "failed", "tombstoned"]


def new_event_id() -> str:
    """Generate a UUIDv7 (RFC 9562) as a 36-char hyphenated hex string.

    UUIDv7 layout (128 bits)::

        unix_ts_ms (48) | ver=0b0111 (4) | rand_a (12)
        | var=0b10 (2)  | rand_b (62)

    The 48-bit Unix-millisecond timestamp prefix makes IDs k-sortable by
    creation time, which is exactly what ADR 017 wants for trace correlation
    and roughly-ordered scans. Randomness comes from ``os.urandom`` so we add
    no new dependency. Within the same millisecond, ordering is random (not
    strictly monotonic) — per-entity strict ordering is provided separately by
    ``event_version`` (see :mod:`projects.lakehouse.events.versioning`).
    """
    unix_ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # low 48 bits
    rand = os.urandom(10)  # 80 bits of randomness; we use 74 of them

    b = bytearray(16)
    # 48-bit big-endian millisecond timestamp.
    b[0] = (unix_ts_ms >> 40) & 0xFF
    b[1] = (unix_ts_ms >> 32) & 0xFF
    b[2] = (unix_ts_ms >> 24) & 0xFF
    b[3] = (unix_ts_ms >> 16) & 0xFF
    b[4] = (unix_ts_ms >> 8) & 0xFF
    b[5] = unix_ts_ms & 0xFF
    # Version nibble 0b0111 in the high nibble of byte 6, rand_a in the rest.
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    # Variant 0b10 in the high bits of byte 8, rand_b fills the remainder.
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9] = rand[3]
    b[10:16] = rand[4:10]

    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def nats_msg_id(entity_id: str, event_version: int) -> str:
    """JetStream dedup key for an event: ``{entity_id}-v{event_version}``.

    Published in the ``Nats-Msg-Id`` header so a duplicate publish of the same
    per-entity version is silently dropped inside the JetStream dedup window
    (ADR 017, idempotency layer 1).
    """
    return f"{entity_id}-v{event_version}"


class EventEnvelope(BaseModel):
    """The ADR-017 domain event envelope.

    Field set is exactly the ADR-017 table; ``correlation_id`` and ``caused_by``
    are the only optional fields. ``schema_version`` defaults to ``1`` (this
    ADR). Extra payload keys are tolerated by consumers per the additive schema
    evolution rules, but the envelope itself forbids unknown top-level fields so
    typos surface immediately.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    entity_type: str
    entity_id: str
    event_type: str
    event_version: int
    event_id: str
    occurred_at: datetime
    producer: str
    payload: dict
    correlation_id: str | None = None
    caused_by: str | None = None


def build_envelope(
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    event_version: int,
    producer: str,
    payload: dict | None = None,
    schema_version: int = 1,
    event_id: str | None = None,
    occurred_at: datetime | None = None,
    correlation_id: str | None = None,
    caused_by: str | None = None,
) -> EventEnvelope:
    """Construct an :class:`EventEnvelope`, filling sensible defaults.

    ``event_id`` defaults to a fresh UUIDv7 (:func:`new_event_id`) and
    ``occurred_at`` to the current UTC wall clock. Callers that compute these
    upstream (e.g. for deterministic replay) may pass them explicitly.
    """
    return EventEnvelope(
        schema_version=schema_version,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        event_version=event_version,
        event_id=event_id or new_event_id(),
        occurred_at=occurred_at or datetime.now(timezone.utc),
        producer=producer,
        payload=payload if payload is not None else {},
        correlation_id=correlation_id,
        caused_by=caused_by,
    )
