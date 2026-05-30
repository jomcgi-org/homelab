"""Async NATS JetStream client wrapper for the lakehouse event stream (ADR 016).

Thin async facade over ``nats`` (nats-py 2.14) JetStream that:

  * resolves the in-cluster NATS URL from ``NATS_URL`` (else cluster default),
  * publishes with a ``Nats-Msg-Id`` header so JetStream dedup gives at-least-once
    delivery + idempotent effect (ADR 017),
  * exposes a durable pull-consumer (consumer-group) helper with a small
    ``fetch`` wrapper.

``NatsClient.publish`` structurally satisfies the ``Publisher`` protocol that the
``events`` package defines; this package deliberately does not import ``events``
(parallel-unit independence) — it just matches the shape.
"""

from projects.lakehouse.nats_client.client import (
    DEFAULT_URL,
    NatsClient,
    resolve_url,
)

__all__ = ["DEFAULT_URL", "NatsClient", "resolve_url"]
