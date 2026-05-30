"""Auto-discovery loader for lakehouse Temporal workflows (ADR agents/015).

Each workflow module dropped under this package exports module-level
``WORKFLOWS`` (a list of ``@temporalio.workflow.defn`` classes) and an optional
``ACTIVITIES`` (a list of activity callables). New workflows = a new file here;
the worker entrypoint (:mod:`projects.lakehouse.orchestrator.workflows.run`)
discovers them through this loader, so there is **no shared registration list**
to edit — that's what keeps the per-workflow Wavefront-3 units conflict-free.

The loader itself imports neither ``temporalio`` nor the workflow modules'
heavy deps at definition time beyond ``importlib``; it just walks the package
and reads the modules' ``WORKFLOWS``/``ACTIVITIES`` attributes.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

# Modules in this package that are NOT workflow modules.
_NON_WORKFLOW_MODULES = {"run"}


def _iter_workflow_modules() -> list[ModuleType]:
    """Import and return every workflow module in this package."""
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(__path__, prefix=__name__ + "."):
        leaf = info.name.rsplit(".", 1)[-1]
        if (
            leaf.startswith("_")
            or leaf.endswith("_test")
            or leaf in _NON_WORKFLOW_MODULES
        ):
            continue
        modules.append(importlib.import_module(info.name))
    return modules


def discover_workflows() -> list[type]:
    """Collect all ``@workflow.defn`` classes exported by workflow modules."""
    found: list[type] = []
    for module in _iter_workflow_modules():
        found.extend(getattr(module, "WORKFLOWS", ()))
    return found


def discover_activities() -> list[object]:
    """Collect all activity callables exported by workflow modules."""
    found: list[object] = []
    for module in _iter_workflow_modules():
        found.extend(getattr(module, "ACTIVITIES", ()))
    return found
