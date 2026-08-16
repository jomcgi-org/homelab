"""FastMonolith module export for the shotter domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

from framework import Module as _Module


def _register_mcp() -> None:
    """Attach the shotter MCP tools to the shared FastMCP instance.

    Unlike sandbox.mcp (which registers via a bare ``@mcp.tool`` decorator at
    import time), shotter.mcp exposes an explicit ``register_mcp_tools()``
    so the BDD specs can call it directly and deterministically instead of
    relying on import-time side effects. This wrapper calls it, mirroring
    the shape every other module's ``register_mcp`` has (an import alone
    would not actually attach the tool).
    """
    from shotter.mcp import register_mcp_tools  # noqa: PLC0415

    register_mcp_tools()


MODULE = _Module(
    name="shotter",
    register_mcp=_register_mcp,
)
