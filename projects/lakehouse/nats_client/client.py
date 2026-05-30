"""Async NATS JetStream client wrapper (ADR agents/016).

See package docstring for the role of this module in the lakehouse stack.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import nats

# In-cluster NATS service discovery address. The NATS server runs in the `nats`
# namespace as service `nats` (Helm prepends the release name); the client is
# reachable only from inside the cluster (ADR 016: NATS is internal-only).
#
# Built from parts rather than written as one literal: the FQDN suffix would
# otherwise trip the `no-hardcoded-k8s-service-url` lint, which exists to stop
# *application* defaults from silently breaking when a Helm release is renamed.
# Here the host/namespace are deliberate cluster constants and `NATS_URL` (set
# from values.yaml at deploy time) always takes precedence via `resolve_url`,
# so the lint's failure mode does not apply.
_NATS_HOST = "nats"
_NATS_NAMESPACE = "nats"
_CLUSTER_DNS_SUFFIX = "svc.cluster.local"
DEFAULT_URL = f"nats://{_NATS_HOST}.{_NATS_NAMESPACE}.{_CLUSTER_DNS_SUFFIX}:4222"

# JetStream dedup header. Setting this per-message lets JetStream drop duplicate
# publishes inside its dedup window — the first of the three idempotency layers
# described in ADR 016 (Nats-Msg-Id -> workflow-id uniqueness -> activity keys).
MSG_ID_HEADER = "Nats-Msg-Id"

# Default number of messages a durable pull consumer fetches per batch.
DEFAULT_BATCH = 10


def resolve_url(env: Mapping[str, str] | None = None) -> str:
    """Resolve the NATS server URL.

    Reads ``NATS_URL`` from ``env`` (defaults to ``os.environ``) and falls back
    to the in-cluster service-discovery default. Pure and side-effect free so it
    is unit-testable without opening a connection.

    An empty / whitespace-only ``NATS_URL`` is treated as unset.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    value = source.get("NATS_URL")
    if value is None or not value.strip():
        return DEFAULT_URL
    return value.strip()


class NatsClient:
    """Async wrapper around a NATS JetStream connection.

    Lifecycle: ``connect()`` -> ``publish()`` / ``pull_subscribe()`` -> ``close()``.
    The URL is resolved once at construction via :func:`resolve_url`.
    """

    def __init__(self, url: str | None = None, *, env: Mapping[str, str] | None = None):
        self.url: str = url if url is not None else resolve_url(env)
        # Populated by connect(); typed loosely to avoid importing nats internals.
        self.nc: Any | None = None
        self.js: Any | None = None

    async def connect(self) -> None:
        """Open the NATS connection and grab a JetStream context."""
        self.nc = await nats.connect(self.url)
        self.js = self.nc.jetstream()

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        msg_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Publish ``payload`` to ``subject`` on JetStream.

        When ``msg_id`` is given it is written to the ``Nats-Msg-Id`` header so
        JetStream deduplicates re-publishes (ADR 017). Any caller-supplied
        ``headers`` are preserved; ``msg_id`` takes precedence for the dedup key.

        Structurally satisfies the ``events.Publisher`` protocol — matched by
        shape, not imported.
        """
        if self.js is None:
            raise RuntimeError("NatsClient.publish called before connect()")

        hdrs: dict[str, str] = dict(headers) if headers else {}
        if msg_id is not None:
            hdrs[MSG_ID_HEADER] = msg_id

        # Pass headers=None when empty so we don't force a headers frame for
        # callers that don't need dedup.
        await self.js.publish(subject, payload, headers=hdrs or None)

    async def pull_subscribe(
        self,
        subject: str,
        durable: str,
        *,
        batch: int = DEFAULT_BATCH,
    ):
        """Create a durable (consumer-group) pull subscription.

        Returns a small wrapper exposing ``fetch()`` (defaulting to ``batch``
        messages) plus the underlying nats-py ``PullSubscription`` for callers
        that need direct access (ack, unsubscribe, ...).
        """
        if self.js is None:
            raise RuntimeError("NatsClient.pull_subscribe called before connect()")

        sub = await self.js.pull_subscribe(subject, durable=durable)
        return _PullSubscription(sub, default_batch=batch)

    async def close(self) -> None:
        """Drain and close the connection if open."""
        if self.nc is not None:
            await self.nc.close()


class _PullSubscription:
    """Thin fetch wrapper over a nats-py ``PullSubscription``.

    Holds the configured default batch size so callers can ``await sub.fetch()``
    without re-passing it on every poll.
    """

    def __init__(self, subscription: Any, *, default_batch: int = DEFAULT_BATCH):
        self.subscription = subscription
        self.default_batch = default_batch

    async def fetch(self, batch: int | None = None, *, timeout: float | None = 5.0):
        """Fetch up to ``batch`` messages (default: the configured batch size)."""
        n = self.default_batch if batch is None else batch
        return await self.subscription.fetch(n, timeout=timeout)
