"""FastMonolith module export for the home domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import home as _domain

from framework import Module as _Module


async def _startup(app):
    """Prime the observability snapshots (topology + stats) once at startup so
    the first request has data; the scheduled rollup jobs refresh thereafter.
    Runs on every replica (best-effort)."""
    from home.observability.rollup import prime_snapshots

    await prime_snapshots()


MODULE = _Module(
    name="home",
    register=_domain.register,
    register_public=_domain.register_public,
    startup=_startup,
)
