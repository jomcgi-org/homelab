"""FastMonolith module export for the cluster domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

from framework import Module as _Module


async def _leader_start(app):
    """Start the cd probe writer (lazy import keeps __init__ light)."""
    from cluster.cd_leader import leader_start  # noqa: PLC0415

    return await leader_start(app)


def _register_mcp() -> None:
    """Attach the cluster MCP tools to the shared instance (side-effect import)."""
    import cluster.mcp  # noqa: F401, PLC0415


MODULE = _Module(
    name="cluster",
    register_mcp=_register_mcp,
    # This domain COMPUTES cd health (it owns the k8s and ArgoCD read surface)
    # but does not serve it. The check runs here on a leader-elected loop and
    # writes the platform_probe latch; the component that reports it is
    # registered on home.module, which composes into the public tier that
    # UptimeRobot actually polls. See core/platform_probe.py for why the
    # handoff exists at all.
    leader_start=_leader_start,
)
