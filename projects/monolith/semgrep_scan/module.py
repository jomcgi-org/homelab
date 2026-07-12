"""FastMonolith module export for the semgrep_scan domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import semgrep_scan as _domain

from framework import Module as _Module


def _register_mcp() -> None:
    """Attach the semgrep_scan MCP tools to the shared instance (side-effect import)."""
    import semgrep_scan.mcp  # noqa: F401, PLC0415


MODULE = _Module(
    name="semgrep_scan",
    register=_domain.register,
    register_mcp=_register_mcp,
)
