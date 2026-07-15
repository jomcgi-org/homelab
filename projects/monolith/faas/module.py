"""FastMonolith module export for the faas domain (see framework/core.py).

Private-only in R1: no ``register_public`` (the ingestion API is an
authenticated author surface; public invocation is a later PR, Task 13). Lives
in its own file (not __init__.py) so importing the domain package does not pull
the framework or FastAPI.
"""

import faas as _domain

from framework import Module as _Module


MODULE = _Module(
    name="faas",
    register=_domain.register,
)
