"""Narrow Kubernetes observation surface for the isolated agents tier."""

from agent_kubernetes.cluster_snapshot import cluster_snapshot
from agent_kubernetes.mcp import kubernetes_pod_logs, kubernetes_read, verify_deployment

__all__ = [
    "cluster_snapshot",
    "kubernetes_pod_logs",
    "kubernetes_read",
    "verify_deployment",
]
