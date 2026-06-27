"""The agent-thread backstop (ADR 022, Phase 4).

A periodic sweep over the registry that catches what the in-VM wrapper missed:
threads stuck in RUNNING far longer than any real turn (the wrapper never
signalled idle, or its idle signal was lost), so an operator can investigate
instead of leaking a microVM. Intended to be run on a 15-30 minute cadence by a
scheduled routine that calls the ``monolith-agent-run-agent-backstop`` MCP tool.

The warm-base refresh half of the backstop (bump requested_sha when a repo's main
advances) is exposed separately via ``agent.base_snapshots.request_rebuild``,
called by the routine with each repo's current main sha.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from app.db import get_engine

_STUCK_COLUMNS = ("thread_id", "repo", "node", "last_active_at")


def find_stuck_threads(session: Session, threshold_secs: int) -> list[dict]:
    """RUNNING threads whose last_active_at is older than threshold_secs.

    These are threads the wrapper should have idled (or completed) by now; a long
    RUNNING stretch with no activity means the idle signal was missed or the
    harness hung. Pure read so it is unit-testable against a session.
    """
    sql = text(
        """
        SELECT thread_id, repo, node, last_active_at
          FROM claude_agent.agent_threads
         WHERE state = 'RUNNING'
           AND last_active_at + (:threshold || ' seconds')::interval < now()
         ORDER BY last_active_at
        """
    )
    rows = session.execute(sql, {"threshold": threshold_secs}).fetchall()
    return [{c: getattr(r, c) for c in _STUCK_COLUMNS} for r in rows]


def _serialize_stuck(row: dict) -> dict:
    out: dict[str, Any] = dict(row)
    la = row.get("last_active_at")
    out["last_active_at"] = la.isoformat() if la else None
    return out


def sweep(threshold_secs: int = 3600) -> dict:
    """Run the stuck-thread sweep and return a summary.

    Opens its own session (callable from the MCP tool's worker thread). Does not
    mutate state - parking/alerting policy is left to the caller so the sweep
    stays a safe read.
    """
    with Session(get_engine()) as session:
        stuck = find_stuck_threads(session, threshold_secs)
    return {
        "threshold_secs": threshold_secs,
        "stuck_count": len(stuck),
        "stuck_threads": [_serialize_stuck(s) for s in stuck],
    }
