"""Transport-agnostic event publishing (ADR agents/017 + agents/016).

This module deliberately does **not** import the NATS client. The concrete
JetStream wrapper lives in the sibling ``projects.lakehouse.nats_client`` unit;
to stay independent of it (these units ship in parallel) we depend only on a
structural :class:`Publisher` protocol. The NATS wrapper satisfies the protocol
by duck typing — no inheritance, no import edge in either direction.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from projects.lakehouse.events.envelope import EventEnvelope, nats_msg_id

# entity_type -> NATS subject. Matches the ADR-016 ``events.knowledge.*``
# subject hierarchy. New entity types add an entry here (and a registry module).
SUBJECT_BY_ENTITY: dict[str, str] = {
    "gap": "events.knowledge.gap",
    "note": "events.knowledge.note",
    "edge": "events.knowledge.edge",
}


@runtime_checkable
class Publisher(Protocol):
    """Anything that can publish raw bytes to a NATS subject.

    The ``projects.lakehouse.nats_client`` JetStream wrapper implements this.
    ``msg_id`` populates the JetStream ``Nats-Msg-Id`` dedup key; ``headers``
    carries that and any additional NATS headers.
    """

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        msg_id: str | None = None,
        headers: dict | None = None,
    ) -> None: ...


def subject_for(envelope: EventEnvelope) -> str:
    """Resolve the NATS subject for ``envelope`` from its ``entity_type``.

    Raises :class:`KeyError` for an unknown entity type so a missing subject
    mapping fails loudly at publish time rather than silently routing nowhere.
    """
    try:
        return SUBJECT_BY_ENTITY[envelope.entity_type]
    except KeyError as exc:
        raise KeyError(
            f"no NATS subject mapped for entity_type={envelope.entity_type!r}; "
            f"add it to SUBJECT_BY_ENTITY"
        ) from exc


async def publish_event(
    publisher: Publisher,
    envelope: EventEnvelope,
    *,
    subject: str | None = None,
) -> None:
    """Serialize ``envelope`` to JSON bytes and publish it via ``publisher``.

    Subject is the explicit ``subject`` argument or, if omitted, derived from
    the envelope's ``entity_type`` via :func:`subject_for`. The JetStream dedup
    key (``{entity_id}-v{event_version}``) is passed both as ``msg_id`` and in
    the ``Nats-Msg-Id`` header so the transport dedups duplicate publishes
    (ADR 017 idempotency layer 1).
    """
    resolved_subject = subject if subject is not None else subject_for(envelope)
    msg_id = nats_msg_id(envelope.entity_id, envelope.event_version)
    payload = envelope.model_dump_json().encode("utf-8")
    await publisher.publish(
        resolved_subject,
        payload,
        msg_id=msg_id,
        headers={"Nats-Msg-Id": msg_id},
    )
