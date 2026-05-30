"""Temporal client connection helpers (ADR agents/015).

``resolve_target`` is pure and testable; ``get_client`` performs the actual
async connection. Both default to the in-cluster frontend gRPC endpoint, which
ADR 015 §Security mandates is internal-only (no external clients).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import temporalio.client

from projects.lakehouse.orchestrator import DEFAULT_NAMESPACE, DEFAULT_TARGET


def resolve_target(env: Mapping[str, str] | None = None) -> str:
    """Resolve the Temporal frontend gRPC target.

    Reads ``TEMPORAL_TARGET`` from ``env`` (defaults to ``os.environ``), falling
    back to the in-cluster frontend (``DEFAULT_TARGET``). Pure: no I/O, no
    connection — safe to call and assert on in tests.

    An empty / whitespace-only ``TEMPORAL_TARGET`` is treated as unset so a
    blank env var in a manifest doesn't produce an unusable target.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    value = source.get("TEMPORAL_TARGET")
    if value is not None and value.strip():
        return value.strip()
    return DEFAULT_TARGET


async def get_client(
    target: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
) -> temporalio.client.Client:
    """Connect to Temporal and return a client.

    When ``target`` is ``None`` the endpoint is resolved from the environment
    via :func:`resolve_target`; otherwise the explicit ``target`` is used as-is.

    TODO(ADR 015 Open Q5): wire the OpenTelemetry interceptor here once the
    Python SDK's trace-context propagation to activities is verified
    (``interceptors=[TracingInterceptor()]`` via
    ``temporalio.contrib.opentelemetry``). Deliberately not implemented in this
    skeleton unit — left as a single, documented wiring point.
    """
    resolved = resolve_target() if target is None else target
    return await temporalio.client.Client.connect(resolved, namespace=namespace)
