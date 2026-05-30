"""Artifact-ready dispatcher: a secondary reaction hook on ``events.serving.artifact-ready``.

ADR 016 §"Per-consumer interpretation": multiple consumers can subscribe to the
same subject and interpret events differently. This dispatcher is the **optional,
secondary** reaction to a new serving artifact.

Why this is a stub (read before adding logic here)
---------------------------------------------------
The quack-server pod **already self-subscribes** to ``events.serving.artifact-ready``
and performs its own ``ATTACH OR REPLACE`` hot-swap directly
(``projects/lakehouse/quack-server/server.py`` :func:`run_swap_consumer`, durable
``quack-serving-swap``). The primary artifact->serving swap is therefore owned by
quack itself, by topology — it does **not** go through Temporal or this dispatcher.

To avoid double-swapping we deliberately do **not** start a swap workflow here.
This dispatcher exists as a clearly-documented seam for *other* reactions to a new
artifact (metrics, notifications, an Iceberg snapshot hook, ...) that a later unit
might want without touching the producer or quack. Today it is a minimal
observability stub: it logs the artifact-ready event. Its own **distinct** durable
(``artifact-ready-dispatcher``, not quack's ``quack-serving-swap``) means JetStream
fans the event out to both consumers independently — the stub's ack never affects
quack's swap.

This dispatcher reacts to **all** ``event_type`` values on the subject (no
filter) because "an artifact is ready" is itself the event; there is no
``created`` vs ``updated`` distinction for the serving stub to care about yet.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from projects.lakehouse.dispatchers import Dispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only
    import temporalio.client

    from projects.lakehouse.events.envelope import EventEnvelope

logger = logging.getLogger(__name__)

# Subject the builder workflow publishes a new serving artifact on. Kept as a
# local constant (rather than imported from quack-server, which is a hyphenated
# binary package with no importable module name) but deliberately identical to
# quack-server's ARTIFACT_READY_SUBJECT.
SUBJECT = "events.serving.artifact-ready"

# DISTINCT durable from quack-server's ``quack-serving-swap`` — JetStream treats
# each durable as an independent consumer group, so this hook and quack's swap
# each receive the event and ack independently. Sharing the durable would steal
# deliveries from quack and break the hot-swap.
DURABLE = "artifact-ready-dispatcher"


async def handle_artifact_ready(
    envelope: EventEnvelope,
    temporal_client: temporalio.client.Client,
) -> None:
    """Secondary reaction to an artifact-ready event (stub: log only).

    Deliberately does **not** start a swap workflow: quack-server owns the
    primary ATTACH-OR-REPLACE swap via its own self-subscription (see module
    docstring). ``temporal_client`` is part of the handler contract (every
    dispatcher gets one) and is intentionally unused here — a future reaction
    (e.g. a metrics or snapshot workflow) would start it via this client.
    """
    payload = getattr(envelope, "payload", {}) or {}
    artifact = payload.get("artifact_url") or payload.get("path")
    logger.info(
        "artifact-ready (secondary hook): entity_id=%s version=%s artifact=%s "
        "(quack-server performs the authoritative hot-swap)",
        getattr(envelope, "entity_id", None),
        payload.get("version"),
        artifact,
    )


DISPATCHERS = [
    Dispatcher(
        subject=SUBJECT,
        durable=DURABLE,
        handle=handle_artifact_ready,
        event_type=None,  # react to every event on the subject
    ),
]
