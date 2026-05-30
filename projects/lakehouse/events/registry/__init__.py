"""Payload schema registry with module auto-discovery (ADR agents/017).

Maps ``entity_type -> {event_type -> payload model}``. Adding a new event family
is "drop a module into this package" — there is no central list to edit. At
import time the loader walks every sibling submodule with
:func:`pkgutil.iter_modules`, imports it, and lets each module register its
payload models into the shared :data:`SCHEMAS` dict via a module-level
``register(SCHEMAS)`` hook.

A module contributes schemas by defining::

    def register(schemas: dict) -> None:
        schemas.setdefault("gap", {})["created"] = GapCreatedPayload

The loader is idempotent — re-import is a no-op because Python caches modules
and ``register`` overwrites the same keys.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

# entity_type -> {event_type -> pydantic payload model class}
SCHEMAS: dict[str, dict[str, Any]] = {}


def _load() -> None:
    """Import every submodule of this package and run its ``register`` hook."""
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        if module_info.name.endswith("_test"):
            # Don't import test modules during normal registry loading.
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        register = getattr(module, "register", None)
        if callable(register):
            register(SCHEMAS)


def payload_model(entity_type: str, event_type: str) -> Any | None:
    """Return the registered payload model for ``(entity_type, event_type)``.

    Returns ``None`` when no model is registered — consumers tolerate unknown
    event types per the ADR-017 additive schema rules.
    """
    return SCHEMAS.get(entity_type, {}).get(event_type)


_load()

__all__ = ["SCHEMAS", "payload_model"]
