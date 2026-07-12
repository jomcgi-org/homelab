"""FastMonolith module export for the agent domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

from framework import Module as _Module


def _register_mcp() -> None:
    """Attach the agent MCP tools to the shared instance (side-effect import)."""
    import agent.mcp  # noqa: F401, PLC0415


MODULE = _Module(
    name="agent",
    register_mcp=_register_mcp,
)
