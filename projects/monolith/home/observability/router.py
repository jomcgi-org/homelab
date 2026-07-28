"""Public, read-only observability endpoint (``/stats``).

The endpoint serves a precomputed snapshot row out of the ``observability``
schema: it never touches ClickHouse or the K8s API, so this module stays free of
the ClickHouse client and SLO math. The writer that fills the stats snapshot
runs only on the private monolith via ``home.observability.rollup``. Keeping this
split lets the public service mount this route without the ClickHouse import
closure (ADR 004 Layer 1+4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session, text

from core.db import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/home/observability", tags=["observability"])


@router.get("/stats", tags=["stats"])
def get_stats(session: Session = Depends(get_session)):
    """Return the latest precomputed stats snapshot (ADR 004).

    The snapshot is refreshed by observability.stats_rollup; this read never
    touches ClickHouse or the K8s API, so the public service can serve it from
    the read replica with no extra credentials.
    """
    row = session.execute(
        text("SELECT payload FROM observability.stats_snapshot WHERE id = 1")
    ).first()
    return row[0] if row else {}
