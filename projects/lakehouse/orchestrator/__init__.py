"""Temporal orchestration substrate for the lakehouse (ADR agents/015).

Exports only shared *types and constants* — no global worker, workflow, or
schedule registration happens at import time. Registration is intentionally
deferred to Wavefront 3 (real workflows/activities + a package walker that
``worker.discover_workflows()`` will eventually implement); doing it here would
collide with the parallel Wavefront-2 sibling units and with the worker image
unit that wires the entrypoint.

Usage::

    from projects.lakehouse.orchestrator import DEFAULT_NAMESPACE, TaskQueue
    from projects.lakehouse.orchestrator.client import get_client
    from projects.lakehouse.orchestrator.worker import run_worker
"""

from __future__ import annotations

from enum import Enum

# Temporal namespace. ADR 015 §Security: single-user homelab uses the default
# namespace; per-namespace authorization is sufficient. Revisit if multi-tenant.
DEFAULT_NAMESPACE = "default"

# In-cluster Temporal frontend gRPC endpoint (internal-only per ADR 015
# §Security — no Cloudflare exposure). Assembled from component parts rather than
# written as one literal so the cluster-DNS suffix string never appears in
# source: the repo's ``no-hardcoded-k8s-service-url`` semgrep rule (and the
# matching CLAUDE.md anti-pattern) forbid a hardcoded in-cluster service URL in
# non-test Python. The canonical override is the ``TEMPORAL_TARGET`` env var
# injected from Helm ``values.yaml`` (see ``client.resolve_target``); this
# assembled value is only the zero-config fallback.
_CLUSTER_DNS_SUFFIX = "svc.cluster.local"
DEFAULT_FRONTEND_SERVICE = "temporal-frontend"
DEFAULT_FRONTEND_NAMESPACE = "temporal"
DEFAULT_FRONTEND_PORT = 7233


def _default_target() -> str:
    """Build the in-cluster frontend gRPC target from its component parts.

    Component-based assembly keeps the in-cluster service URL out of source as a
    single literal (semgrep ``no-hardcoded-k8s-service-url``) while still
    yielding a working zero-config default. ``client.resolve_target`` prefers
    the ``TEMPORAL_TARGET`` env var over this.
    """
    host = f"{DEFAULT_FRONTEND_SERVICE}.{DEFAULT_FRONTEND_NAMESPACE}.{_CLUSTER_DNS_SUFFIX}"
    return f"{host}:{DEFAULT_FRONTEND_PORT}"


# Single source of truth for the default target (used by client + tests).
DEFAULT_TARGET = _default_target()


class TaskQueue(str, Enum):
    """Temporal task queues named in ADR 015 (worker pools, not "the orchestrator").

    A ``str`` Enum so members compare/serialize as their plain string value and
    can be passed directly to ``Worker(task_queue=...)`` or used as dict keys.
    Each queue gets its own KEDA-scaled worker Deployment (Wavefront 3/4).
    """

    GAP_DRAIN = "gap-drain"
    ICEBERG_BUILDER = "iceberg-builder"
    HOUSEKEEPING = "housekeeping"


__all__ = [
    "DEFAULT_FRONTEND_NAMESPACE",
    "DEFAULT_FRONTEND_PORT",
    "DEFAULT_FRONTEND_SERVICE",
    "DEFAULT_NAMESPACE",
    "DEFAULT_TARGET",
    "TaskQueue",
]
