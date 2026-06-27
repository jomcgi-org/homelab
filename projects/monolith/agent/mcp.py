"""MCP tools for the claude-routine-agent surface.

Twenty-two tools across seven families — each is a thin async wrapper that
calls into the corresponding ``agent.*`` operation module and serializes
datetimes / UUIDs as strings for JSON transport. The Python function
names use underscores; FastMCP's wire identifiers use the dashed form
(``monolith-agent-acquire-lock`` etc.).

  Locks    : acquire_lock, extend_lock, release_lock, list_locks
  Notify   : notify
  Check    : check_stuck_jobs, check_orphan_jobs, check_dead_letters,
             check_firing_alerts
  Trigger  : trigger_job  (scheduler.scheduled_jobs)
  Routine  : list_routine_jobs, claim_routine_job, complete_routine_job,
             register_routine_job, deregister_routine_job,
             trigger_routine_job  (claude_agent.routine_jobs)
  Threads  : list_agent_threads, get_agent_thread, resume_agent_thread
             (claude_agent.agent_threads, ADR 022)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from agent import backstop, base_snapshots, checks, locks
from agent import notify as notify_mod
from agent import routine_jobs, threads
from app.mcp_app import mcp


def _iso(dt: datetime | None) -> str | None:
    """Serialize a ``datetime`` to ISO 8601, or return None if absent."""
    return dt.isoformat() if dt else None


def _serialize_lock(row: dict) -> dict:
    return {
        "key": row["key"],
        "holder": row["holder"],
        "acquired_at": _iso(row["acquired_at"]),
        "expires_at": _iso(row["expires_at"]),
    }


def _serialize_routine_job(row: dict) -> dict:
    return {
        **row,
        "next_run_at": _iso(row.get("next_run_at")),
        "last_run_at": _iso(row.get("last_run_at")),
        "locked_at": _iso(row.get("locked_at")),
        "created_at": _iso(row.get("created_at")),
    }


def _serialize_scheduled_job(row: dict) -> dict:
    return {
        **row,
        "next_run_at": _iso(row.get("next_run_at")),
        "last_run_at": _iso(row.get("last_run_at")),
        "locked_at": _iso(row.get("locked_at")),
    }


def _serialize_dead_letter(row: dict) -> dict:
    return {**row, "last_failed_at": _iso(row.get("last_failed_at"))}


# --- Locks ---------------------------------------------------------------


@mcp.tool
async def monolith_agent_acquire_lock(key: str, holder: str, ttl_secs: int) -> dict:
    """Acquire an opportunistic TTL lock keyed by ``key``.

    Steals expired locks but refuses live ones. Returns ``acquired=False``
    when another holder has the key with a still-live TTL.
    """
    result = locks.acquire(key, holder, ttl_secs)
    return {
        "acquired": result.acquired,
        "lock_id": str(result.lock_id) if result.lock_id else None,
        "expires_at": _iso(result.expires_at),
    }


@mcp.tool
async def monolith_agent_extend_lock(lock_id: str, ttl_secs: int) -> dict:
    """Extend an existing lock by ``ttl_secs`` from now.

    Returns ``ok=False`` if the lock no longer exists or has been
    re-acquired by someone else (different ``lock_id``).
    """
    new_expires = locks.extend(UUID(lock_id), ttl_secs)
    return {
        "ok": new_expires is not None,
        "new_expires_at": _iso(new_expires),
    }


@mcp.tool
async def monolith_agent_release_lock(lock_id: str) -> dict:
    """Release a held lock by ``lock_id``."""
    return {"ok": locks.release(UUID(lock_id))}


@mcp.tool
async def monolith_agent_list_locks(prefix: str | None = None) -> dict:
    """List currently-held (unexpired) locks, optionally filtered by key prefix."""
    rows = locks.list_active(prefix)
    return {"locks": [_serialize_lock(r) for r in rows]}


# --- Notify --------------------------------------------------------------


@mcp.tool
async def monolith_agent_notify(
    message: str,
    level: Literal["info", "warn", "error"] = "info",
    channel: str | None = None,
) -> dict:
    """Post a Discord message via the in-process bot.

    Defaults to the homelab channel from settings. The ``channel`` arg,
    if specified, must be in the configured allow-list.
    """
    return await notify_mod.notify(message, level=level, channel=channel)


# --- Check ---------------------------------------------------------------


@mcp.tool
async def monolith_agent_check_stuck_jobs(threshold_mins: int = 10) -> dict:
    """Scheduler rows whose lock has held longer than ``threshold_mins`` minutes."""
    rows = checks.check_stuck_jobs(threshold_mins)
    return {"jobs": [_serialize_scheduled_job(r) for r in rows]}


@mcp.tool
async def monolith_agent_check_orphan_jobs() -> dict:
    """Scheduler rows whose ``name`` has no registered handler in-process."""
    rows = checks.check_orphan_jobs()
    return {"jobs": [_serialize_scheduled_job(r) for r in rows]}


@mcp.tool
async def monolith_agent_check_dead_letters(limit: int = 20) -> dict:
    """Raw inputs that exhausted all gardener retry attempts."""
    rows = checks.check_dead_letters(limit)
    return {"raws": [_serialize_dead_letter(r) for r in rows]}


@mcp.tool
async def monolith_agent_check_firing_alerts() -> dict:
    """SigNoz alert rules currently in the ``firing`` state."""
    rows = await checks.check_firing_alerts()
    return {"alerts": rows}


# --- Trigger (scheduler.scheduled_jobs) ----------------------------------


@mcp.tool
async def monolith_agent_trigger_job(name: str) -> dict:
    """Kick a ``scheduler.scheduled_jobs`` row to run on the next tick."""
    return {"ok": checks.trigger_job(name)}


# --- Routine jobs (claude_agent.routine_jobs) ----------------------------


@mcp.tool
async def monolith_agent_list_routine_jobs(
    due_only: bool = False, kind: str | None = None
) -> dict:
    """List routine_jobs rows, optionally filtered to due-only and/or by kind."""
    rows = routine_jobs.list_jobs(due_only=due_only, kind=kind)
    return {"jobs": [_serialize_routine_job(r) for r in rows]}


@mcp.tool
async def monolith_agent_claim_routine_job(
    holder: str,
    ttl_secs: int,
    kind: str | None = None,
    name: str | None = None,
) -> dict:
    """Claim a routine_jobs row.

    With ``name`` set, claims that specific row. Without ``name``,
    claims the next due unclaimed row, optionally filtered by ``kind``.
    Returns ``claimed=False`` and ``job=None`` when nothing is available.
    """
    job = routine_jobs.claim_job(holder, ttl_secs, kind=kind, name=name)
    return {
        "claimed": job is not None,
        "job": _serialize_routine_job(job) if job else None,
    }


@mcp.tool
async def monolith_agent_complete_routine_job(
    name: str, status: str, summary: str | None = None
) -> dict:
    """Mark a claimed routine_jobs row complete and clear its lock."""
    return {"ok": routine_jobs.complete_job(name, status, summary=summary)}


@mcp.tool
async def monolith_agent_register_routine_job(
    name: str,
    kind: str,
    interval_secs: int | None = None,
    payload: dict | None = None,
    next_run_at: str | None = None,
    created_by: str = "unknown",
) -> dict:
    """Insert a new routine_jobs row.

    ``next_run_at`` accepts an ISO 8601 string, parsed with
    ``datetime.fromisoformat``. Raises ``ValueError`` if a row with the
    given ``name`` already exists.
    """
    parsed_next = datetime.fromisoformat(next_run_at) if next_run_at else None
    try:
        routine_jobs.register_job(
            name=name,
            kind=kind,
            interval_secs=interval_secs,
            payload=payload,
            next_run_at=parsed_next,
            created_by=created_by,
        )
    except IntegrityError as exc:
        raise ValueError(f"Routine job {name!r} already exists") from exc
    return {"ok": True}


@mcp.tool
async def monolith_agent_deregister_routine_job(name: str) -> dict:
    """Delete a routine_jobs row."""
    return {"ok": routine_jobs.deregister_job(name)}


@mcp.tool
async def monolith_agent_trigger_routine_job(name: str) -> dict:
    """Kick a routine_jobs row to immediately due by setting ``next_run_at = now()``."""
    return {"ok": routine_jobs.trigger_job(name)}


# --- Agent threads (Firecracker snapshot/restore catalog, ADR 022) -------


@mcp.tool
async def monolith_agent_list_agent_threads(
    state: str | None = None,
    node: str | None = None,
) -> dict:
    """List Firecracker agent threads, newest-active first.

    Optionally filter by lifecycle ``state`` (PENDING, RUNNING, IDLE,
    COMPLETED, FAILED) and/or ``node``.
    """
    rows = threads.list_threads(state=state, node=node)
    return {"threads": [threads.serialize(r) for r in rows]}


@mcp.tool
async def monolith_agent_get_agent_thread(thread_id: str) -> dict:
    """Get one Firecracker agent thread by its stable thread id."""
    row = threads.get_thread(thread_id)
    return {"thread": threads.serialize(row) if row else None}


@mcp.tool
async def monolith_agent_resume_agent_thread(thread_id: str) -> dict:
    """Request the controller restore an IDLE agent thread.

    Stamps a wake request the reconcile loop picks up. Only IDLE threads are
    resumable. For any other state this returns ok=False with the current state.
    """
    return threads.request_resume(thread_id)


# --- Warm bases + backstop (ADR 022, Phase 4) ----------------------------


@mcp.tool
async def monolith_agent_list_agent_bases() -> dict:
    """List the per-repo warm-base snapshots and their build status."""
    rows = base_snapshots.list_bases()
    return {"bases": [base_snapshots.serialize(r) for r in rows]}


@mcp.tool
async def monolith_agent_request_base_rebuild(
    repo: str, arch: str, main_sha: str
) -> dict:
    """Request the controller rebuild a repo's warm base at ``main_sha``.

    Call when a repo's main advances. Idempotent: a repeat at the same sha is a
    no-op for the controller.
    """
    return base_snapshots.request_rebuild(repo, arch, main_sha)


@mcp.tool
async def monolith_agent_run_agent_backstop(stuck_threshold_mins: int = 60) -> dict:
    """Sweep for stuck RUNNING threads and alert if any are found.

    A thread RUNNING with no activity for longer than the threshold likely missed
    its idle signal or hung. Posts a single Discord alert when any are found.
    Intended to be run every 15-30 minutes by a scheduled routine.
    """
    summary = await asyncio.to_thread(backstop.sweep, stuck_threshold_mins * 60)
    if summary["stuck_count"]:
        ids = ", ".join(s["thread_id"] for s in summary["stuck_threads"])
        await notify_mod.notify(
            f"agent-thread backstop: {summary['stuck_count']} stuck thread(s): {ids}",
            level="warn",
        )
    return summary
