"""Per-domain Iceberg table schemas, auto-discovered into the ``TABLES`` registry.

Each submodule of this package defines exactly one Iceberg table and exposes two
module-level attributes:

  TABLE_NAME   str            the table's name within the warehouse namespace
  SCHEMA       pyiceberg.Schema   its column schema

At import time we walk every submodule with ``pkgutil.iter_modules`` and collect
``{TABLE_NAME: SCHEMA}`` into ``TABLES``. Adding a new domain table is therefore
"drop a new file in this package" — no edit to this loader or any registry list.

``TABLES`` maps table name -> pyiceberg ``Schema`` (e.g. ``"note_events"`` ->
``Schema(...)``). Consumers (the batch-commit workflow, table-creation bootstrap)
iterate it to ensure-create tables and to resolve a name to its schema.
"""

from __future__ import annotations

import importlib
import pkgutil

from pyiceberg.schema import Schema

TABLES: dict[str, Schema] = {}


def _discover() -> None:
    """Import every submodule and register its (TABLE_NAME, SCHEMA) pair."""
    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name
        if name.startswith("_") or name.endswith("_test"):
            continue
        module = importlib.import_module(f"{__name__}.{name}")
        table_name = getattr(module, "TABLE_NAME", None)
        schema = getattr(module, "SCHEMA", None)
        if table_name is None or schema is None:
            # Not a table module (helpers, etc.) — skip silently.
            continue
        TABLES[table_name] = schema


_discover()
