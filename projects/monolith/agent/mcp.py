"""MCP tools for the claude-routine-agent surface.

Thin async wrappers that call into the corresponding operation module
(``agent.*`` for locks/checks/routines or ``chat.directives`` for the
directive-autopilot surface) and serialize datetimes
/ UUIDs as strings for JSON transport. The Python function names use
underscores; FastMCP's wire identifiers use the dashed form
(``monolith-agent-acquire-lock`` etc.).

  Locks    : acquire_lock, extend_lock, release_lock, list_locks
  Notify   : notify
  Check    : check_stuck_jobs, check_orphan_jobs, check_dead_letters,
             check_firing_alerts
  Trigger  : trigger_job  (scheduler.scheduled_jobs)
  Routine  : list_routine_jobs, claim_routine_job, complete_routine_job,
             register_routine_job, deregister_routine_job,
             trigger_routine_job  (claude_agent.routine_jobs)
  Directives: chat_list_directives, chat_directive_history, chat_set_directive,
             chat_pin_directive, chat_revert_directive (introspect + tune the
             silent directive autopilot, manual writes win precedence)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from agent import checks, locks
from agent import notify as notify_mod
from agent import routine_jobs
from core.mcp_app import mcp
import chat.api as chat_api


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

    Defaults to the homelab channel from settings. The ``channel`` arg, if
    specified, must be a channel in a server the bot has been added to.
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


# --- Chat directive introspection + tuning (the directive-autopilot surface) --
#
# Out-of-band review and manual tuning for the silent directive autopilot: the
# autopilot never announces in Discord, so these tools ARE the review channel.
# A manual set or pin here writes the manual provenance source, which the
# autopilot treats as a hard precedence winner (it will not override a manual
# row within its cooldown). All DB work runs in a worker thread, own session.


@mcp.tool
async def monolith_chat_list_directives() -> dict:
    """List the active per-channel behavioural directives and per-user style
    preferences, each with its provenance source and version and its most recent
    directive-autopilot action (status plus rationale). This is the review
    surface for the silent autopilot, which never announces in Discord. A source
    of manual means a human pinned or set it and the autopilot will leave it
    alone.
    """
    return await asyncio.to_thread(chat_api.list_directives)


@mcp.tool
async def monolith_chat_directive_history(scope_kind: str, scope_id: str) -> dict:
    """Show the full version history and directive-autopilot action log for one
    scope. scope_kind is channel or user, and scope_id is the channel id or user
    id. The versions list is the complete history oldest first, and autopilot_log
    is every autonomous action the autopilot took on this scope with its baseline
    and evidence, so an apply and its later keep or revert can be traced.
    """
    return await asyncio.to_thread(chat_api.directive_history, scope_kind, scope_id)


@mcp.tool
async def monolith_chat_set_directive(
    scope_kind: str, scope_id: str, text: str
) -> dict:
    """Manually set a behavioural directive or user style preference, marking it
    source manual so the autopilot will not override it within its cooldown.
    scope_kind is channel or user. The text is screened by the same tone-only
    guard the autopilot uses: a text that reads as changing tools, permissions,
    acls, ambient mode, repos, or admin access is rejected with a reason and
    nothing is written. Returns ok true on success.
    """
    return await asyncio.to_thread(chat_api.set_directive, scope_kind, scope_id, text)


@mcp.tool
async def monolith_chat_pin_directive(scope_kind: str, scope_id: str) -> dict:
    """Pin the active directive or style preference for a scope by marking its
    active row source manual, so the autopilot leaves it alone. scope_kind is
    channel or user. This stamps provenance only and does not change the
    directive text. Returns ok false when the scope has no active row yet.
    """
    return await asyncio.to_thread(chat_api.pin_directive, scope_kind, scope_id)


@mcp.tool
async def monolith_chat_revert_directive(scope_kind: str, scope_id: str) -> dict:
    """Manually revert a scope to its prior directive or style preference,
    reinstating the previous version as a fresh active row marked source manual.
    scope_kind is channel or user. Returns ok false with a reason when there is
    no prior version to restore.
    """
    return await asyncio.to_thread(chat_api.revert_directive, scope_kind, scope_id)


@mcp.tool
async def monolith_chat_trust_status(guild_id: str = "") -> dict:
    """Bosun trust-safeguards ledger snapshot: every tracked user with their
    effective decay-applied trust score, lockout state, and signal counts, plus
    the newest trained forest (version, status, metrics). A locked_out user
    gets no engagement from the bot until their score recovers or they are
    pardoned. Pass guild_id to filter to one server. Empty lists all guilds.
    """
    return await asyncio.to_thread(chat_api.trust_status, guild_id)


@mcp.tool
async def monolith_chat_trust_pardon(
    guild_id: str, user_id: str, pardoned_by: str = "mcp"
) -> dict:
    """Pardon a user on the Bosun trust-safeguards ledger: reset their score to
    100 (immediately ending any lockout) and flip their recent labeled
    moderation events to clean, so a wrong lockout becomes corrective training
    data instead of a poisoned example. Returns ok false when the user has no
    ledger row yet.
    """
    return await asyncio.to_thread(chat_api.pardon_user, guild_id, user_id, pardoned_by)
