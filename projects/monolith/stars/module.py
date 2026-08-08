"""FastMonolith module export for the stars domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import stars as _domain
from stars.health import stars_health

from framework import Module as _Module


MODULE = _Module(
    name="stars",
    register=_domain.register,
    register_public=_domain.register_public,
    register_health={"stars": stars_health},
)
