"""Cluster domain public API: the only surface other domains may import.

Other domains must import the Kubernetes client from ``cluster.api`` (the
homepage observability stats endpoint does), never from ``cluster`` internals
such as ``cluster.kubernetes`` or ``cluster.mcp``. The client retrieves cluster
state (node/pod/deployment counts, ArgoCD app status, node resource
aggregates), so the ``cluster`` domain owns it; consumers depend on this domain
rather than the reverse.
"""

from __future__ import annotations

from cluster.kubernetes import (  # re-exported
    RESOURCE_KINDS,
    KubernetesClient,
    UnknownKindError,
)
from cluster.summarize import (  # re-exported
    build_health,
    dedupe_events,
    filter_logs,
    resource_detail,
    resource_row,
)

__all__ = [
    "KubernetesClient",
    "RESOURCE_KINDS",
    "UnknownKindError",
    "build_health",
    "dedupe_events",
    "filter_logs",
    "resource_detail",
    "resource_row",
]
