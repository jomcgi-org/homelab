"""FastMonolith module export for the private updates journal."""

import updates as _domain

from framework import Module as _Module


def _register_mcp() -> None:
    """Attach the one structured product-update submission tool."""
    from updates.mcp import register_mcp_tools

    register_mcp_tools()


MODULE = _Module(
    name="updates",
    register=_domain.register,
    register_mcp=_register_mcp,
)
