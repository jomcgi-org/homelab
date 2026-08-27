"""Scheduled rollup that snapshots the public stats payload into Postgres
(ADR 004 Layer 4).

The private monolith is the only DCGM exporter and Kubernetes caller: this job builds
the full payload with ``build_stats`` and upserts it into the ``observability``
schema. The read endpoint (and the Phase 5 public service) then reads the snapshot
row, so nothing scrapes DCGM or queries the K8s API at request time.

Follows the monolith scheduled-handler rule: do the network I/O with ``await`` first,
then delegate the synchronous DB write to a worker thread with its own session.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlmodel import Session, text

from core.db import get_engine
from home.observability.stats import build_stats
from scheduler.api import register_job

logger = logging.getLogger(__name__)

# Stats is cheap and changes often (pod counts, GPU), so refresh it frequently.
STATS_ROLLUP_INTERVAL_SECS = 120


def _write_stats_snapshot(payload: dict) -> None:
    with Session(get_engine()) as session:
        session.execute(
            text(
                """
                INSERT INTO observability.stats_snapshot (id, payload, snapshot_at)
                VALUES (1, :payload, now())
                ON CONFLICT (id) DO UPDATE
                    SET payload = EXCLUDED.payload, snapshot_at = EXCLUDED.snapshot_at
                """
            ),
            {"payload": json.dumps(payload)},
        )
        session.commit()


async def stats_rollup() -> None:
    """Build the stats payload (DCGM + K8s) and snapshot it to Postgres."""
    payload = await build_stats()
    await asyncio.to_thread(_write_stats_snapshot, payload)
    logger.info("Stats snapshot refreshed")


def register(session: Session) -> None:
    """Register the rollup job. Called from home.on_startup_jobs."""
    register_job(
        session,
        name="observability.stats_rollup",
        interval_secs=STATS_ROLLUP_INTERVAL_SECS,
        handler=lambda _: stats_rollup(),
    )


async def prime_snapshots() -> None:
    """Best-effort initial snapshot at startup so the first request has data."""
    try:
        await stats_rollup()
    except Exception:
        logger.exception("Initial stats snapshot failed; scheduler will retry")
