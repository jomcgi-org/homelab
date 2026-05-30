"""Unit tests for transport-agnostic event publishing (ADR agents/017).

Async paths are driven with ``asyncio.run`` rather than the pytest-asyncio
plugin so the test is fully hermetic and self-contained (no conftest /
asyncio_mode dependency).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from projects.lakehouse.events.envelope import build_envelope
from projects.lakehouse.events.publish import (
    SUBJECT_BY_ENTITY,
    Publisher,
    publish_event,
    subject_for,
)


class FakePublisher:
    """Captures the args of the most recent publish call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        msg_id: str | None = None,
        headers: dict | None = None,
    ) -> None:
        self.calls.append(
            {
                "subject": subject,
                "payload": payload,
                "msg_id": msg_id,
                "headers": headers,
            }
        )


def test_fake_publisher_satisfies_protocol():
    # Structural typing: the fake duck-types the Publisher protocol.
    assert isinstance(FakePublisher(), Publisher)


def test_subject_for_known_entities():
    for entity_type, subject in SUBJECT_BY_ENTITY.items():
        env = build_envelope(
            entity_type=entity_type,
            entity_id="x-1",
            event_type="created",
            event_version=1,
            producer="p",
            payload={},
        )
        assert subject_for(env) == subject


def test_subject_for_unknown_entity_raises():
    env = build_envelope(
        entity_type="unknown",
        entity_id="x-1",
        event_type="created",
        event_version=1,
        producer="p",
        payload={},
    )
    with pytest.raises(KeyError):
        subject_for(env)


def test_publish_event_derives_subject_and_msg_id():
    pub = FakePublisher()
    env = build_envelope(
        entity_type="gap",
        entity_id="gap-42",
        event_type="created",
        event_version=3,
        producer="monolith.gardener",
        payload={"topic": "t"},
    )

    asyncio.run(publish_event(pub, env))

    assert len(pub.calls) == 1
    call = pub.calls[0]
    assert call["subject"] == "events.knowledge.gap"
    assert call["msg_id"] == "gap-42-v3"
    assert call["headers"] == {"Nats-Msg-Id": "gap-42-v3"}
    # Payload is the JSON-serialized envelope.
    decoded = json.loads(call["payload"].decode("utf-8"))
    assert decoded["entity_id"] == "gap-42"
    assert decoded["event_version"] == 3
    assert decoded["payload"] == {"topic": "t"}


def test_publish_event_subject_override():
    pub = FakePublisher()
    env = build_envelope(
        entity_type="gap",
        entity_id="gap-42",
        event_type="created",
        event_version=1,
        producer="p",
        payload={},
    )

    asyncio.run(publish_event(pub, env, subject="events.custom.override"))

    assert pub.calls[0]["subject"] == "events.custom.override"
    # msg_id still derives from the envelope, not the subject.
    assert pub.calls[0]["msg_id"] == "gap-42-v1"
