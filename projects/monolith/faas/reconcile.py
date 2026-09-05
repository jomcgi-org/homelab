"""Reconcile registry-owned FaaS Workload custom resources."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import select

from faas import workload
from faas.models import Function

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """Summary of one orphan Workload reconciliation sweep."""

    scanned: int
    orphans: list[str]
    deleted: list[str]
    kept: list[str]
    skipped_unmarked: int


def _registered_function_names() -> set[str]:
    """Load all registry names through the application's session helper."""
    # Imported lazily: a module-level core.db import freezes DATABASE_URL at
    # import time and breaks test collection that never touches a database.
    from core.db import get_session

    sessions = get_session()
    session = next(sessions)
    try:
        return set(session.exec(select(Function.name)).all())
    finally:
        sessions.close()


async def reconcile_orphan_workloads(*, dry_run: bool = False) -> ReconcileReport:
    """Delete marked Workload CRs that have no matching Function row."""
    api = await workload._custom_objects_api()
    response = await api.list_namespaced_custom_object(
        group=workload.GROUP,
        version=workload.VERSION,
        namespace=workload.NAMESPACE,
        plural=workload.PLURAL,
    )
    items = response.get("items") or []
    function_names = _registered_function_names()

    orphans: list[str] = []
    deleted: list[str] = []
    kept: list[str] = []
    skipped_unmarked = 0

    for item in items:
        metadata = item.get("metadata") or {}
        labels = metadata.get("labels") or {}
        if labels.get(workload.MANAGED_BY_LABEL) != workload.MANAGED_BY_VALUE:
            skipped_unmarked += 1
            continue

        name = metadata["name"]
        if name in function_names:
            kept.append(name)
            continue

        orphans.append(name)
        if dry_run:
            logger.info("faas orphan workload name=%s deleted=false dry_run=true", name)
            continue

        # A single failed delete must not stop the sweep.
        try:
            await workload.delete_workload(name)
        except Exception:  # noqa: BLE001
            logger.error(
                "faas orphan workload name=%s deleted=false", name, exc_info=True
            )
        else:
            deleted.append(name)
            logger.info("faas orphan workload name=%s deleted=true", name)

    return ReconcileReport(
        scanned=len(items),
        orphans=orphans,
        deleted=deleted,
        kept=kept,
        skipped_unmarked=skipped_unmarked,
    )
