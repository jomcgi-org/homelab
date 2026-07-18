"""FastMonolith module export for the ember_public domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI, mirroring chat_public/module.py and
demos/module.py.
"""

import ember_public as _domain

from framework import Module as _Module


MODULE = _Module(
    name="ember_public",
    register=_domain.register,
    register_public=_domain.register_public,
)
