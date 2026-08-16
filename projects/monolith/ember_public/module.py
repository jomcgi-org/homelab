"""FastMonolith module export for the ember_public domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI, mirroring chat_public/module.py and
demos/module.py.
"""

import ember_public as _domain
from ember_public.health import (
    EMBER_QWEN_STALENESS_S,
    EMBER_SYNTHETIC_STALENESS_S,
    synthetic_probe_health,
)

from framework import Module as _Module


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
        # The qwen session synthetic. probe_qwen has been writing this latch
        # since it landed, but nothing read it, so a failing qwen turn was
        # invisible to health. Registered here on its OWN staleness because its
        # CronWorkflow is hourly, not the 5-minute one (see EMBER_QWEN_*).
        "ember_qwen": synthetic_probe_health("qwen", EMBER_QWEN_STALENESS_S),
    },
)
