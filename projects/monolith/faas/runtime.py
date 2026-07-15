"""Registration-time runtime contract for the zip lane (ADR 045 / embervm R1).

The runtime-python base image bakes a fixed dependency subset; a function's
declared requirements must be a subset of what the base ships (there is no
install step at prep time, standing decision 4 of the R1 plan). This module is
the monolith-side copy of that contract: the ingestion API (Task 10) rejects a
registration whose declared imports are not all baked.

SOURCE OF TRUTH is the runtime image README and its apko.lock.json
(projects/embervm/runtimes/python/README.md, "Baked dependency subset"). This
frozenset mirrors that table's importable top-level module names. It is a
registration-time convenience check, not the enforcement point: the shim's
import at build time (readyPath gating) and the Task 10 smoke run are what
actually prove a function runs. Keep this in sync when the base image's baked
set changes.
"""

from __future__ import annotations

# Importable top-level names the runtime-python base ships on top of stdlib.
# Mirrors projects/embervm/runtimes/python/README.md "Baked dependency subset":
# pandas (+ numpy transitively), matplotlib, scipy, pillow (imported as PIL),
# pyyaml (imported as yaml), python-dateutil (imported as dateutil).
BAKED_PACKAGES: frozenset[str] = frozenset(
    {
        "pandas",
        "numpy",
        "matplotlib",
        "scipy",
        "PIL",
        "yaml",
        "dateutil",
    }
)

# The runtime enum is currently one entry (Python only in R1, standing decision
# 2); adding Node is a data change here plus a CRD enum addition.
KNOWN_RUNTIMES: frozenset[str] = frozenset({"python312"})
