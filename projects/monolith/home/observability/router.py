"""Public, read-only observability endpoint (``/stats``).

The endpoint serves a precomputed snapshot row out of the ``observability``
schema: it never touches DCGM or the K8s API, so this module stays free of
external metrics clients and SLO math. The writer that fills the stats snapshot
runs only on the private monolith via ``home.observability.rollup``. Keeping this
split lets the public service mount this route without private metrics imports
closure (ADR 004 Layer 1+4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from sqlmodel import Session, text

from core.db import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/home/observability", tags=["observability"])
_STATS_CACHE_CONTROL = (
    "public, s-maxage=60, stale-while-revalidate=86400, "
    "stale-if-error=31536000"
)


@router.get("/stats", tags=["stats"])
def get_stats(response: Response, session: Session = Depends(get_session)):
    """Return the latest precomputed stats snapshot (ADR 004).

    The snapshot is refreshed by observability.stats_rollup; this read never
    touches DCGM or the K8s API, so the public service can serve it from
    the read replica with no extra credentials.
    """
    row = session.execute(
        text("SELECT payload FROM observability.stats_snapshot WHERE id = 1")
    ).first()
    payload = row[0] if row else {}
    response.headers["Cache-Control"] = _STATS_CACHE_CONTROL
    return payload
