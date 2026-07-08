"""Public, read-only observability endpoints (``/stats`` and ``/topology``).

Both endpoints serve precomputed snapshot rows out of the ``observability``
schema: they never touch ClickHouse or the K8s API, so this module stays free of
the ClickHouse client, SLO math, and topology config. The writer that fills those
snapshots (``build_topology``) lives in ``home.observability.topology_query`` and
runs only on the private monolith via ``home.observability.rollup``. Keeping this
split lets the public service mount these routes without the ClickHouse import
closure (ADR 004 Layer 1+4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session, text

from app.db import get_session

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


@router.get("/topology")
def get_topology(session: Session = Depends(get_session)):
    """Return the latest precomputed topology snapshot (ADR 004).

    Refreshed by observability.topology_rollup. Returns an empty skeleton until
    the first rollup has run (the public page tolerates this with fallback data).
    """
    row = session.execute(
        text("SELECT payload FROM observability.topology_snapshot WHERE id = 1")
    ).first()
    return row[0] if row else {"groups": [], "nodes": [], "edges": []}
