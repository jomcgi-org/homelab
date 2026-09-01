"""FastMonolith module export for the ember_public domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI, mirroring chat_public/module.py and
demos/module.py.
"""

import ember_public as _domain
from ember_public.durability import build_durability_health
from ember_public.health import EMBER_SYNTHETIC_STALENESS_S, synthetic_probe_health

from framework import Module as _Module

# The ember-durability component (#4338, ADR embervm/031): a FATAL-tier check
# reading the CP's /v1/health/durability surface (export-failure streaks +
# gc-manifest stall), never an advisory: both durability tiers end in the
# health surface. Dark while EMBER_DURABILITY_HEALTH_URL is unset, per the
# standing rule that a health-affecting detector lands suspend:true and flips
# on only after live verification; the flip is a values/env change only.
_DURABILITY_CHECK = build_durability_health()


MODULE = _Module(
    name="ember_public",
    register=_domain.register,
    register_public=_domain.register_public,
    register_health={
        "ember_bazel": synthetic_probe_health("bazel", EMBER_SYNTHETIC_STALENESS_S),
        "ember_semgrep": synthetic_probe_health("semgrep", EMBER_SYNTHETIC_STALENESS_S),
        "ember_pages": synthetic_probe_health("pages", EMBER_SYNTHETIC_STALENESS_S),
        "ember_postgres": synthetic_probe_health(
            "postgres", EMBER_SYNTHETIC_STALENESS_S
        ),
        **({"ember-durability": _DURABILITY_CHECK} if _DURABILITY_CHECK else {}),
    },
)
