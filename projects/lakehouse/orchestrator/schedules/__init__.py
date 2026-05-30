"""Temporal schedule definitions for the lakehouse (ADR agents/015).

ADR 015 §"Scheduling unified with execution": cron-style triggers become
Temporal ``Schedule``s registered idempotently from startup, sharing the same
Postgres persistence, retry policy, and UI as event-triggered workflows. The
per-domain scheduler module shrinks to "register Temporal Schedules from a
``SCHEDULES`` list at boot."

Each schedule lives in its own module here exporting a module-level
``SCHEDULES`` list of :class:`ScheduleDefinition`. New schedule = a new file;
:func:`all_schedules` aggregates them via ``pkgutil`` discovery, so there is
**no shared registration list** to edit (conflict-free, mirroring the
``workflows`` loader). :func:`register_schedules` is called from monolith/worker
startup (Wavefront 5) to idempotently create each schedule.

Schedules reference their target workflow **by type-name string**
(``ScheduleActionStartWorkflow(workflow="...")``), so this package does not
import the workflow classes and stays independent of the parallel
WF-STORAGE / WF-DOMAIN units.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import pkgutil
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import temporalio.client

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ScheduleDefinition:
    """A single schedule: its stable ID plus the Temporal ``Schedule`` spec.

    ``schedule`` is typed loosely (``object``) so importing this module — and
    running the hermetic loader tests — never forces a ``temporalio`` import at
    module-definition time when it isn't needed. The submodules that build the
    actual specs import ``temporalio.client`` themselves.
    """

    schedule_id: str
    schedule: object


def _iter_schedule_modules() -> list[ModuleType]:
    """Import and return every schedule module in this package."""
    modules: list[ModuleType] = []
    for info in pkgutil.iter_modules(__path__, prefix=__name__ + "."):
        leaf = info.name.rsplit(".", 1)[-1]
        if leaf.startswith("_") or leaf.endswith("_test"):
            continue
        modules.append(importlib.import_module(info.name))
    return modules


def all_schedules() -> list[ScheduleDefinition]:
    """Aggregate the ``SCHEDULES`` list exported by every schedule module."""
    found: list[ScheduleDefinition] = []
    for module in _iter_schedule_modules():
        found.extend(getattr(module, "SCHEDULES", ()))
    return found


async def register_schedules(
    client: temporalio.client.Client,
    *,
    namespace: str = "default",
) -> None:
    """Idempotently create every discovered schedule on ``client``.

    Called once from monolith/worker startup (Wavefront 5). For each
    :class:`ScheduleDefinition`, calls ``client.create_schedule(id, schedule)``.
    If a schedule with that ID already exists (a prior boot created it),
    Temporal raises :class:`temporalio.client.ScheduleAlreadyRunningError`,
    which is swallowed — that's what makes startup registration idempotent:
    re-running it on every boot is a no-op for already-registered schedules.

    ``namespace`` is accepted for symmetry with the rest of the orchestrator
    surface and logged; the schedule is created in whatever namespace ``client``
    is connected to (Temporal has no per-call namespace override on
    ``create_schedule``), so callers should connect the client to ``namespace``.
    """
    # Imported lazily so module import (and the hermetic loader tests) don't
    # require temporalio to be importable.
    import temporalio.client

    for definition in all_schedules():
        try:
            await client.create_schedule(definition.schedule_id, definition.schedule)
            logger.info(
                "registered Temporal schedule %s (namespace=%s)",
                definition.schedule_id,
                namespace,
            )
        except temporalio.client.ScheduleAlreadyRunningError:
            # Already registered by a prior boot — idempotent no-op.
            logger.debug(
                "Temporal schedule %s already exists; skipping (namespace=%s)",
                definition.schedule_id,
                namespace,
            )


__all__ = [
    "ScheduleDefinition",
    "all_schedules",
    "register_schedules",
]
