"""FastMonolith module export for the moving domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
glob only their own sources.
"""

import moving as _domain

from framework import Module as _Module


MODULE = _Module(
    name="moving",
    register=_domain.register,
)
