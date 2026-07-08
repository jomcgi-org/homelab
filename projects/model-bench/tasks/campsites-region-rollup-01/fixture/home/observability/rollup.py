"""Scheduled rollups that snapshot the public topology and stats payloads into
Postgres (ADR 004 Layer 4).

The private monolith is the only ClickHouse / Kubernetes caller: these jobs build
the full payload with ``build_topology`` / ``build_stats`` and upsert it into the
``observability`` schema. The read endpoints (and the Phase 5 public service) then
read the snapshot row, so nothing queries ClickHouse or the K8s API at request time.

Follows the monolith scheduled-handler rule: do the network I/O with ``await`` first,
then delegate the synchronous DB write to a worker thread with its own session.
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlmodel import Session, text

from app.db import get_engine
from home.observability.stats import build_stats
from home.observability.topology_query import build_topology
from scheduler.api import register_job

logger = logging.getLogger(__name__)

# Topology is ~70 ClickHouse queries; refresh on the old cache_ttl cadence. Stats is
# cheaper and changes more often (pod counts, GPU), so refresh it more frequently.
TOPOLOGY_ROLLUP_INTERVAL_SECS = 900
STATS_ROLLUP_INTERVAL_SECS = 120


def _write_topology_snapshot(payload: dict) -> None:
    with Session(get_engine()) as session:
        session.execute(
            text(
                """
                INSERT INTO observability.topology_snapshot (id, payload, snapshot_at)
                VALUES (1, :payload, now())
                ON CONFLICT (id) DO UPDATE
                    SET payload = EXCLUDED.payload, snapshot_at = EXCLUDED.snapshot_at
                """
            ),
            {"payload": json.dumps(payload)},
        )
        session.commit()


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


async def topology_rollup() -> None:
    """Build the topology payload (ClickHouse) and snapshot it to Postgres."""
    payload = await build_topology()
    await asyncio.to_thread(_write_topology_snapshot, payload)
    logger.info("Topology snapshot refreshed (%d nodes)", len(payload.get("nodes", [])))


async def stats_rollup() -> None:
    """Build the stats payload (ClickHouse + K8s) and snapshot it to Postgres."""
    payload = await build_stats()
    await asyncio.to_thread(_write_stats_snapshot, payload)
    logger.info("Stats snapshot refreshed")


def register(session: Session) -> None:
    """Register the rollup jobs. Called from home.on_startup_jobs."""
    register_job(
        session,
        name="observability.topology_rollup",
        interval_secs=TOPOLOGY_ROLLUP_INTERVAL_SECS,
        handler=lambda _: topology_rollup(),
        # Builds the full topology payload from ClickHouse; co-resident with the
        # graph layout in the OOM that motivated heavy-job serialization.
        heavy=True,
    )
    register_job(
        session,
        name="observability.stats_rollup",
        interval_secs=STATS_ROLLUP_INTERVAL_SECS,
        handler=lambda _: stats_rollup(),
    )


async def prime_snapshots() -> None:
    """Best-effort initial snapshot at startup so the first request has data."""
    try:
        await topology_rollup()
    except Exception:
        logger.exception("Initial topology snapshot failed; scheduler will retry")
    try:
        await stats_rollup()
    except Exception:
        logger.exception("Initial stats snapshot failed; scheduler will retry")
