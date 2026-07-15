"""FastMonolith module export for the faas domain (see framework/core.py).

The private tier mounts the full surface (``register``: the ingestion API +
private invocation router). The public tier mounts ONLY the public invocation
router (``register_public``: ``/functions/<name>`` for ``visibility=public``),
never the ``/api/functions`` ingestion surface (Task 13, standing decision 7).
Lives in its own file (not __init__.py) so importing the domain package does not
pull the framework or FastAPI.
"""

import faas as _domain

from framework import Module as _Module


MODULE = _Module(
    name="faas",
    register=_domain.register,
    register_public=_domain.register_public,
)
