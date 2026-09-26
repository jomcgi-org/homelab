"""Narrow Kubernetes observation surface for the isolated agents tier."""

from agent_kubernetes.mcp import kubernetes_pod_logs, kubernetes_read, verify_deployment

__all__ = ["kubernetes_pod_logs", "kubernetes_read", "verify_deployment"]
