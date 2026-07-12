"""FastMonolith module export for the demos domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import demos as _domain

from framework import Module as _Module


MODULE = _Module(
    name="demos",
    register=_domain.register,
)
