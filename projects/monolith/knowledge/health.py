"""Advisory health for the Luna knowledge extraction lane."""

from __future__ import annotations

import asyncio
from datetime import timezone
import os

from sqlalchemy import text
from sqlmodel import Session

from knowledge.extraction import GARDENER_VERSION, KG_JOB_KIND, KG_NODE_KEY

_STALE_SECONDS = 6 * 60 * 60


def _iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _kg_health_core(session: Session, cap: int) -> dict:
    queue = session.execute(
        text(
            """
            SELECT count(*) AS queued,
                   EXTRACT(EPOCH FROM (now() - MIN(next_run_at))) AS oldest_seconds
              FROM claude_agent.routine_jobs
             WHERE routine_kind = :kind AND next_run_at IS NOT NULL
            """
        ),
        {"kind": KG_JOB_KIND},
    ).one()
    provenance = session.execute(
        text(
            """
            SELECT count(*) FILTER (
                       WHERE derived_note_id = 'failed'
                         AND created_at >= now() - interval '24 hours'
                   ) AS failed_24h,
                   count(*) FILTER (
                       WHERE atom_fk IS NOT NULL
                         AND created_at >= now() - interval '24 hours'
                   ) AS atoms_24h,
                   max(created_at) FILTER (
                       WHERE derived_note_id <> 'failed'
                   ) AS last_success_at
              FROM knowledge.atom_raw_provenance
             WHERE gardener_version = :version
            """
        ),
        {"version": GARDENER_VERSION},
    ).one()
    jobs_today = session.execute(
        text(
            """
            SELECT count(*)
              FROM agent_sessions.agent_sessions
             WHERE node_key = :node_key
               AND created_at >= now() - interval '24 hours'
            """
        ),
        {"node_key": KG_NODE_KEY},
    ).scalar_one()
    oldest = max(0.0, float(queue.oldest_seconds or 0.0))
    return {
        "ok": oldest <= _STALE_SECONDS,
        "queued": int(queue.queued),
        "oldest_queued_seconds": oldest,
        "failed_24h": int(provenance.failed_24h),
        "atoms_24h": int(provenance.atoms_24h),
        "last_success_at": _iso(provenance.last_success_at),
        "jobs_today": int(jobs_today),
        "cap": cap,
    }


def _read_kg_health() -> dict:
    from core.db import get_engine

    cap = int(os.environ.get("DRAINER_KG_MAX_JOBS_PER_DAY", "40"))
    with Session(get_engine()) as session:
        return _kg_health_core(session, cap)


async def kg_health() -> dict:
    return await asyncio.to_thread(_read_kg_health)
