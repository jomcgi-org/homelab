"""Dispatcher process entrypoint (ADR agents/016 §"Workflow dispatchers").

    python -m projects.lakehouse.dispatchers.run

One ``monolith-dispatchers``-style process that subscribes to every discovered
dispatcher's NATS subject and translates events into Temporal ``start_workflow``
calls. Per ADR 016, dispatchers are small adapters; this module is the shared
plumbing they run inside:

* connect a :class:`~projects.lakehouse.nats_client.NatsClient` and a Temporal
  ``temporalio.client.Client`` (``orchestrator.client.get_client``);
* for each :class:`~projects.lakehouse.dispatchers.Dispatcher`, open a durable
  (consumer-group) pull subscription on its subject;
* poll all subscriptions in a graceful loop, decode each message into an
  ADR-017 :class:`EventEnvelope`, apply the dispatcher's ``event_type`` filter,
  and invoke its ``handle(envelope, temporal_client)``.

Message disposition mirrors quack-server's swap consumer:
  * malformed payload (un-decodable envelope) -> ``term`` (redelivery never helps);
  * handler raises -> neither ack nor term, so JetStream redelivers later;
  * handled (or filtered out) -> ``ack``.

This is AUTHOR-ONLY scaffolding for this unit: the per-dispatcher Deployment that
runs this entrypoint lives in the sibling W4-CHART unit; nothing here is wired to
production by this run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from projects.lakehouse.dispatchers import Dispatcher, all_dispatchers
from projects.lakehouse.events.envelope import EventEnvelope
from projects.lakehouse.nats_client import NatsClient
from projects.lakehouse.orchestrator.client import get_client

if TYPE_CHECKING:  # pragma: no cover - typing only
    import temporalio.client

logger = logging.getLogger(__name__)

# How long a single pull fetch blocks before returning empty (idle stream). A
# timeout is normal and simply re-polls; kept short so a stop signal is observed
# promptly across all subscriptions.
POLL_TIMEOUT = 5.0


def decode_envelope(raw: bytes) -> EventEnvelope:
    """Decode a raw NATS message body into an ADR-017 :class:`EventEnvelope`.

    Pure (no I/O) so the dispatch logic is unit-testable without NATS. Raises
    (``pydantic.ValidationError`` / ``ValueError`` from bad JSON) on a malformed
    body so the caller can ``term`` the message.
    """
    return EventEnvelope.model_validate_json(raw)


async def dispatch_message(
    dispatcher: Dispatcher,
    msg: Any,
    temporal_client: temporalio.client.Client,
) -> None:
    """Decode, filter, and handle one message for ``dispatcher``; then dispose.

    Disposition:
      * malformed envelope -> ``term`` (won't redeliver);
      * event filtered out by the dispatcher's ``event_type`` -> ``ack`` (it is
        correctly handled by being ignored — redelivery would never help);
      * handler success -> ``ack``;
      * handler raises -> neither ack nor term, leaving JetStream to redeliver.
    """
    try:
        envelope = decode_envelope(msg.data)
    except Exception:  # noqa: BLE001 — any decode failure is terminal for this msg
        logger.exception(
            "malformed event on %s; terminating message", dispatcher.subject
        )
        await msg.term()
        return

    if not dispatcher.matches(envelope):
        # Not this dispatcher's event_type — ack so it isn't redelivered.
        await msg.ack()
        return

    # Handler success -> ack. A raised handler is intentionally NOT caught here
    # so the message is left un-acked for JetStream redelivery (at-least-once);
    # the per-subscription loop guards against one bad message killing the loop.
    await dispatcher.handle(envelope, temporal_client)
    await msg.ack()


async def run_dispatcher_loop(
    dispatcher: Dispatcher,
    subscription: Any,
    temporal_client: temporalio.client.Client,
    *,
    stop: asyncio.Event | None = None,
    poll_timeout: float = POLL_TIMEOUT,
) -> None:
    """Poll one dispatcher's durable subscription until ``stop`` is set.

    Fetch timeouts (idle stream) are normal and re-poll. A handler that raises is
    logged and the next poll continues — one bad event must not kill the loop
    (the un-acked message is redelivered by JetStream).
    """
    while stop is None or not stop.is_set():
        try:
            msgs = await subscription.fetch(timeout=poll_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            # Idle stream. Yield to the loop first so a synchronously-raising
            # fake fetch (tests / fast-failing server) can't busy-spin and
            # starve the shutdown task — same guard as quack-server's consumer.
            await asyncio.sleep(0)
            continue
        for msg in msgs:
            try:
                await dispatch_message(dispatcher, msg, temporal_client)
            except Exception:  # noqa: BLE001 — keep the loop alive on handler error
                logger.exception(
                    "dispatch failed on %s; leaving message for redelivery",
                    dispatcher.subject,
                )


async def run(
    *,
    dispatchers: list[Dispatcher] | None = None,
    nats_client: NatsClient | None = None,
    temporal_client: temporalio.client.Client | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Wire up every dispatcher and run their poll loops concurrently.

    Connects NATS + Temporal when clients aren't injected (tests inject fakes),
    opens a durable pull subscription per dispatcher, and runs each loop as a
    task until ``stop`` is set (or forever). Cleans up the NATS connection on
    exit. ``dispatchers`` defaults to :func:`all_dispatchers`.
    """
    discovered = all_dispatchers() if dispatchers is None else dispatchers

    nc = NatsClient() if nats_client is None else nats_client
    own_nats = nats_client is None
    if own_nats:
        await nc.connect()

    client = await get_client() if temporal_client is None else temporal_client

    try:
        tasks: list[asyncio.Task] = []
        for dispatcher in discovered:
            subscription = await nc.pull_subscribe(
                dispatcher.subject, dispatcher.durable
            )
            logger.info(
                "subscribed dispatcher subject=%s durable=%s event_type=%s",
                dispatcher.subject,
                dispatcher.durable,
                dispatcher.event_type,
            )
            tasks.append(
                asyncio.create_task(
                    run_dispatcher_loop(dispatcher, subscription, client, stop=stop)
                )
            )
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        if own_nats:
            await nc.close()


def _main() -> None:  # pragma: no cover — process entrypoint (network)
    """Console entrypoint: discover dispatchers and run their loops forever."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    _main()
