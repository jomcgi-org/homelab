"""Per-tick snapshot of factory tasks stuck in the uncertain outcome.

A factory task is uncertain when its start ledger holds a row with
``status='uncertain'`` (``FactoryStart``, ``factory_models.py``). That is the
factory-side half of the condition the execution layer sees as a permit with
``state='uncertain'`` (``AgentCapacityReservation``, ``execution/models.py``),
fingerprinted by ``read_uncertain_factory_attempt`` and swept by
``permit_supervision.py``. The start ledger is the half that names the task:
``record_start_outcome`` (``factory_controls.py``) writes the graph run status
into ``FactoryStart.status`` and stamps ``updated_at`` on that same write, so
``updated_at`` marks when the task became uncertain. The permit row itself
carries no transition timestamp (``created_at`` is the reservation time and
``settled_at`` is only set on settlement), which is why the snapshot reads the
start ledger rather than the permit table.

The conductor ``tick()`` calls :func:`emit_uncertain_task_snapshot` first on
every cycle, including cycles with no uncertain tasks and cycles that return
early otherwise, so the ``factory.tasks.oldest_uncertain_age_seconds`` column
is present on every ``factory.tasks.snapshot`` span. A trigger on a column
that only appears on work spans stays silent exactly when the watched loop
stops emitting, which is the failure the trigger exists to catch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.db import get_engine
from sqlalchemy import distinct, func
from sqlmodel import Session, select

from factory.orchestration.factory_models import FactoryStart
from factory.orchestration.tracing import set_attributes, tracer

logger = logging.getLogger(__name__)

SPAN = "factory.tasks.snapshot"
UNCERTAIN_STATUS = "uncertain"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes for TIMESTAMPTZ columns while Postgres
    # returns aware ones; coerce before comparing so the age matches on both.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def uncertain_task_snapshot(db: Session, *, now: datetime | None = None) -> dict:
    """Count distinct uncertain tasks and age the oldest, in one aggregate SELECT.

    Returns ``{"uncertain_tasks": int, "oldest_uncertain_age_seconds": float}``,
    with both zero when no start row is uncertain. A future-dated ``updated_at``
    (clock skew between writers) clamps to zero rather than going negative.
    """
    current = now if now is not None else _now()
    count, oldest = db.exec(
        select(
            func.count(distinct(FactoryStart.task_id)),
            func.min(FactoryStart.updated_at),
        ).where(FactoryStart.status == UNCERTAIN_STATUS)
    ).one()
    if not count or oldest is None:
        return {"uncertain_tasks": 0, "oldest_uncertain_age_seconds": 0.0}
    age = (current - _as_utc(oldest)).total_seconds()
    return {
        "uncertain_tasks": int(count),
        "oldest_uncertain_age_seconds": max(0.0, float(age)),
    }


def emit_uncertain_task_snapshot() -> dict:
    """Emit one ``factory.tasks.snapshot`` span carrying the current snapshot.

    Always sets both attributes, including 0/0 when nothing is uncertain and
    when the query itself fails. A query failure is logged and recorded on the
    span, and never propagates: snapshot telemetry must not break the
    reconciliation loop that calls it.
    """
    with tracer.start_as_current_span(SPAN) as span:
        try:
            with Session(get_engine()) as db:
                snapshot = uncertain_task_snapshot(db)
        except Exception as exc:
            logger.warning("factory uncertain-task snapshot failed", exc_info=True)
            span.record_exception(exc)
            snapshot = {"uncertain_tasks": 0, "oldest_uncertain_age_seconds": 0.0}
        set_attributes(
            span,
            {
                "factory.tasks.uncertain": snapshot["uncertain_tasks"],
                "factory.tasks.oldest_uncertain_age_seconds": snapshot[
                    "oldest_uncertain_age_seconds"
                ],
            },
        )
        return snapshot
