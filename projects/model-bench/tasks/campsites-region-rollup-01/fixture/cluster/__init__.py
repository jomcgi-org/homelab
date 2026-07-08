"""Curated, token-efficient Kubernetes debug surface for the ``k8s-*`` MCP tools.

Owns the read-only ``KubernetesClient`` (``kubernetes``, exposed to other
domains via ``cluster.api``) and shapes the raw objects into lean,
LLM-friendly summaries (``summarize``) before handing them to MCP clients
(``mcp``).
"""
