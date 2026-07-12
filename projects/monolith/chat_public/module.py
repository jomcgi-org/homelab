"""FastMonolith module export for the chat_public domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import chat_public as _domain

from framework import Module as _Module


MODULE = _Module(
    name="chat_public",
    register_public=_domain.register_public,
)
