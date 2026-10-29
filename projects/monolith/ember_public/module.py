"""FastMonolith module export for the ember_public domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI. Private tier only: the public Ember
exhibits were retired (#6913), so there is no ``register_public``.
"""

import ember_public as _domain
from ember_public.health import (
    EMBER_CODEX_STALENESS_S,
    synthetic_probe_health,
)

from framework import Module as _Module

MODULE = _Module(
    name="ember_public",
    register=_domain.register,
    register_health={
        "ember_codex": synthetic_probe_health("codex", EMBER_CODEX_STALENESS_S),
    },
)
