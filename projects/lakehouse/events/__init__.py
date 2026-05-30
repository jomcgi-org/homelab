"""Domain event envelope + publish helpers (ADR agents/017).

The canonical event schema for the event-sourced lakehouse. Every cross-component
state change is a versioned :class:`~projects.lakehouse.events.envelope.EventEnvelope`
published to a NATS subject. Consumers interpret events by ``event_type``; producers
never mutate state implicitly (tombstones are first-class events).

Public surface::

    from projects.lakehouse.events import (
        EventEnvelope, EventType, build_envelope, new_event_id, nats_msg_id,
        Publisher, publish_event, subject_for, SUBJECT_BY_ENTITY,
        next_event_version, CREATE_TABLE_SQL,
    )

The payload schema registry (``projects.lakehouse.events.registry``) maps
``entity_type -> {event_type -> payload model}`` and auto-discovers new event
families by dropping a module into ``registry/``.
"""

from projects.lakehouse.events.envelope import (
    EventEnvelope,
    EventType,
    build_envelope,
    nats_msg_id,
    new_event_id,
)
from projects.lakehouse.events.publish import (
    SUBJECT_BY_ENTITY,
    Publisher,
    publish_event,
    subject_for,
)
from projects.lakehouse.events.versioning import CREATE_TABLE_SQL, next_event_version

__all__ = [
    "EventEnvelope",
    "EventType",
    "build_envelope",
    "nats_msg_id",
    "new_event_id",
    "Publisher",
    "publish_event",
    "subject_for",
    "SUBJECT_BY_ENTITY",
    "next_event_version",
    "CREATE_TABLE_SQL",
]
