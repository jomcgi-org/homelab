"""FastMonolith module export for the knowledge domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import knowledge as _domain

from framework import Module as _Module


def _register_mcp() -> None:
    """Attach the knowledge MCP tools to the shared private instance."""
    from core.mcp_app import mcp  # noqa: PLC0415
    from knowledge.mcp import register_mcp_tools  # noqa: PLC0415

    register_mcp_tools(mcp)


MODULE = _Module(
    name="knowledge",
    register=_domain.register,
    register_public=_domain.register_public,
    register_mcp=_register_mcp,
)
