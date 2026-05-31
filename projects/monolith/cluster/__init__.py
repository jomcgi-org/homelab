"""Curated, token-efficient Kubernetes debug surface for the ``k8s-*`` MCP tools.

Reuses ``shared.kubernetes.KubernetesClient`` for cluster access and shapes the
raw objects into lean, LLM-friendly summaries (``summarize``) before handing
them to MCP clients (``mcp``).
"""
