"""FastMonolith module export for the cluster domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

from cluster.cd_health import cd_health

from framework import Module as _Module


def _register_mcp() -> None:
    """Attach the cluster MCP tools to the shared instance (side-effect import)."""
    import cluster.mcp  # noqa: F401, PLC0415


MODULE = _Module(
    name="cluster",
    register_mcp=_register_mcp,
    # The cd component lives here rather than in a domain of its own: this
    # domain already owns the k8s and ArgoCD read surface, and a new module
    # would mint a domain image and a MONOLITH_DOMAINS parity obligation for
    # something that mounts no routes. See cd_health.py and issue #4597.
    register_health={"cd": cd_health},
)
