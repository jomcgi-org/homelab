"""MCP tools for the claude-routine-agent surface.

Thin async wrappers that call into the corresponding operation module
(``agent.*`` for locks/checks/routines or ``chat.directives`` for the
directive-autopilot surface) and serialize datetimes
/ UUIDs as strings for JSON transport. The Python function names use
underscores; FastMCP's wire identifiers use the dashed form
(``monolith-agent-acquire-lock`` etc.).

  Locks    : acquire_lock, extend_lock, release_lock, list_locks
  Notify   : notify
  Check    : check_stuck_jobs, check_dead_letters
  Trigger  : trigger_job  (Argo CronWorkflow one-off submission)
  Routine  : list_routine_jobs, claim_routine_job, complete_routine_job,
             register_routine_job, deregister_routine_job,
             trigger_routine_job  (claude_agent.routine_jobs)
  Directives: chat_list_directives, chat_directive_history, chat_set_directive,
             chat_pin_directive, chat_revert_directive (introspect + tune the
             silent directive autopilot, manual writes win precedence)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from agent import checks, locks
from agent import factory_goals as goals_mod
from agent import notify as notify_mod
from agent import routine_jobs
from core.mcp_app import mcp
from auth.api import current_principal
from factory.api import operator_access_error
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


@mcp.tool
async def monolith_agent_schema_retirement_inventory() -> dict:
    """Read fixed database metadata for the feature retirement in PR 7134.

    Requires a standing human operator. Returns bounded object identities,
    dependency closure, retained column signatures and public reader privileges
    from a consistent read-only snapshot. Takes no object names or SQL and reads
    no application rows or object bodies. An incomplete result cannot establish
    absence of dependencies. This does not authorize migration or establish a
    future DDL-free window, consumer compatibility or public route health.
    """
    refusal = operator_access_error(current_principal())
    if refusal:
        return {"complete": False, "error": refusal}
    from agent.schema_retirement import read_inventory

    return await asyncio.to_thread(read_inventory)


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
async def monolith_agent_check_dead_letters(limit: int = 20) -> dict:
    """Raw inputs that exhausted all gardener retry attempts."""
    rows = checks.check_dead_letters(limit)
    return {"raws": [_serialize_dead_letter(r) for r in rows]}


# --- Trigger (scheduler.scheduled_jobs) ----------------------------------


@mcp.tool
async def monolith_agent_trigger_job(name: str) -> dict:
    """Submit the job's Argo CronWorkflow as a one-off Workflow."""
    result = await checks.trigger_job(name)
    return {
        "ok": result.status_code == 202,
        "job": result.job,
        "workflow_name": result.workflow_name,
        "namespace": result.namespace,
        "status_code": result.status_code,
        "message": result.message,
    }


# --- Routine jobs (claude_agent.routine_jobs) ----------------------------


@mcp.tool
async def monolith_agent_list_routine_jobs(
    due_only: bool = False,
    kind: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
    offset: Annotated[int, Field(ge=0, le=1000000)] = 0,
) -> dict:
    """Read a bounded page of routine-job status and lease metadata.

    Filter by due-only or kind and follow next_offset for more rows. Payloads
    and free-form summaries are omitted. A null next_run_at does not prove
    completion: an unknown-outcome hold also prevents automatic retries.
    """

    def read_page():
        rows = routine_jobs.list_jobs(
            due_only=due_only, kind=kind, limit=limit + 1, offset=offset
        )
        fields = (
            "name",
            "routine_kind",
            "interval_secs",
            "next_run_at",
            "last_run_at",
            "last_status",
            "locked_by",
            "locked_at",
            "ttl_secs",
            "created_at",
        )
        jobs = []
        for row in rows[:limit]:
            projected = {key: row.get(key) for key in fields}
            if row.get("name") == "factory-retro-daily":
                payload = row.get("payload")
                if isinstance(payload, dict):
                    import hashlib

                    projected["learning_configuration"] = {
                        "payload_sha256": hashlib.sha256(
                            json.dumps(
                                payload, sort_keys=True, separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                        "model": payload.get("model")
                        if payload.get("model") in {"sol", "luna", "astra"}
                        else None,
                        "stage": payload.get("learning_stage")
                        if payload.get("learning_stage")
                        in {"evidence", "evaluate", "diagnose"}
                        else None,
                        "reconciled": isinstance(
                            payload.get("learning_activation"), dict
                        ),
                    }
            truncated = []
            for key, value in projected.items():
                if isinstance(value, str) and len(value) > 256:
                    projected[key] = value[:256]
                    truncated.append(key)
            jobs.append(
                {**_serialize_routine_job(projected), "truncated_fields": truncated}
            )
        return {
            "jobs": jobs,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    return await asyncio.to_thread(read_page)


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
    """Mark a claimed routine_jobs row complete and clear its lock.

    Records ``status`` (free text, stored as last_status) and, when given,
    ``summary`` as the run's outcome. A recurring row (non-null
    interval_secs) is rescheduled interval_secs from now. A one-shot row is
    left idle until monolith-agent-trigger-routine-job re-arms it. Returns
    ``ok=False`` when no matching row was updated, including a row held for
    an unknown invocation outcome.
    """
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

    ``kind`` is the routine kind that claim and list filter on.
    ``interval_secs`` makes the row recurring. Leave it unset for a one-shot
    row that runs once and then waits to be re-triggered. ``payload`` is
    stored as-is for the claiming worker. For kind qwen-drain (the drain lane)
    the payload is an object with a required prompt and optional repo, branch
    and reasoning. ``next_run_at`` accepts an ISO 8601 string, parsed with
    ``datetime.fromisoformat``, and orders claims, so an earlier timestamp
    runs first. Names are unique forever, completed one-shots included, so a
    re-run needs a fresh name such as a dated prefix. Raises ``ValueError``
    if a row with the given ``name`` already exists. docs/agents/drain-queue.md
    covers writing drain-lane prompts.
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
async def monolith_agent_trigger_routine_job(
    name: str, recovery: dict | None = None
) -> dict:
    """Kick a routine_jobs row to immediately due by setting ``next_run_at = now()``.

    This re-arms a completed one-shot. A drain-lane job that failed on a bad
    guest (session_down, workspace does not exist, a dropped connection) is
    terminal as a one-shot and usually passes when triggered again.

    Unknown outcomes remain held unless recovery is explicitly supplied for
    kg-fact-review by a standing human operator. Preview takes mode=preview,
    session_id and permit_id. Apply takes the same IDs, mode=apply, the returned
    expected_state_sha256, a unique reconciliation_key and acknowledgment equal
    to accept_duplicate_compute_and_writes. This abandons only unfinished review
    claims, preserves receipts and execution history, and rearms under existing
    caps. Capacity stays charged unless a fresh control-plane read proves the
    exact dispatch ceased. No successful result is invented. Never use this risk acceptance as automatic retry policy.
    """
    if recovery is not None:
        principal = current_principal()
        refusal = operator_access_error(principal)
        if refusal:
            return {"ok": False, "error": refusal}
        if name != "kg-fact-review":
            raise ValueError("Explicit recovery is limited to kg-fact-review")
        from agent.routine_reconciliation import recover_review_job

        return await asyncio.to_thread(
            recover_review_job, actor=principal.subject, recovery=recovery
        )
    return {"ok": routine_jobs.trigger_job(name)}


async def _recover_fact_review(recovery: dict) -> dict:
    principal = current_principal()
    refusal = operator_access_error(principal)
    if refusal:
        return {"ok": False, "error": refusal}
    from agent.routine_reconciliation import recover_review_job

    return await asyncio.to_thread(
        recover_review_job, actor=principal.subject, recovery=recovery
    )


@mcp.tool
async def monolith_agent_preview_fact_review_recovery(
    session_id: Annotated[int, Field(strict=True, gt=0)],
    permit_id: Annotated[int, Field(strict=True, gt=0)],
) -> dict:
    """Read an exact held kg-fact-review attempt and preview operator recovery.

    Requires a standing human operator. Does not schedule work or change claims,
    permits, receipts or audit history. Returns expected_state_sha256 for the
    separate apply tool. Session and permit identity are checked server-side.
    """
    return await _recover_fact_review(
        {"mode": "preview", "session_id": session_id, "permit_id": permit_id}
    )


@mcp.tool
async def monolith_agent_apply_fact_review_recovery(
    session_id: Annotated[int, Field(strict=True, gt=0)],
    permit_id: Annotated[int, Field(strict=True, gt=0)],
    expected_state_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    reconciliation_key: str,
    acknowledgment: Literal["accept_duplicate_compute_and_writes"],
) -> dict:
    """Apply one digest-bound operator recovery of a held kg-fact-review attempt.

    Requires a standing human operator and the digest from the separate preview
    tool. Preserves completed receipts and prior state, abandons only unfinished
    claims, and rearms under existing caps. Capacity remains charged unless fresh
    server evidence proves cessation. The reconciliation key makes replay
    idempotent. Explicit acknowledgment accepts duplicate computation and writes
    for this attempt only. Never use this as automatic retry policy.
    """
    return await _recover_fact_review(
        {
            "mode": "apply",
            "session_id": session_id,
            "permit_id": permit_id,
            "expected_state_sha256": expected_state_sha256,
            "reconciliation_key": reconciliation_key,
            "acknowledgment": acknowledgment,
        }
    )


@mcp.tool
async def monolith_agent_set_factory_goals(
    goals: list[dict], declared_by: str = "opus"
) -> dict:
    """Replace the active factory goal set shown on the factory page.

    Each goal carries a one line statement plus linked issue numbers. The
    previous set is retired and the new set goes live at once. At most five
    goals stay active. Progress is scored from merged pull requests at read
    time. Use this when the orchestrator changes what the factory is working
    towards.
    """
    return await asyncio.to_thread(goals_mod.replace_goals, goals, declared_by)


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


async def _recover_completed_raw(recovery: dict) -> dict:
    principal = current_principal()
    refusal = operator_access_error(principal)
    if refusal:
        return {"ok": False, "error": refusal}
    from agent.routine_reconciliation import recover_completed_raw

    return await asyncio.to_thread(
        recover_completed_raw, actor=principal.subject, recovery=recovery
    )


@mcp.tool
async def monolith_agent_preview_completed_raw_recovery(
    job_name: str,
    session_id: Annotated[int, Field(strict=True, gt=0)],
    permit_id: Annotated[int, Field(strict=True, gt=0)],
) -> dict:
    """Preview one locked, completed raw KG result as a standing human operator.

    No raw content is returned. Does not apply, unlock, invoke or schedule work.
    Error, unknown, already-applied and ambiguous ownership remain fenced.
    """
    return await _recover_completed_raw(
        {
            "mode": "preview",
            "job_name": job_name,
            "session_id": session_id,
            "permit_id": permit_id,
        }
    )


@mcp.tool
async def monolith_agent_apply_completed_raw_recovery(
    job_name: str,
    session_id: Annotated[int, Field(strict=True, gt=0)],
    permit_id: Annotated[int, Field(strict=True, gt=0)],
    expected_state_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    reconciliation_key: str,
) -> dict:
    """Apply one previewed saved raw KG result as a standing human operator.

    Requires the default-off completedRawRecoveryEnabled deployment gate.
    Revalidates exact original-result and ownership identity, then atomically
    validates/applies extraction, completes its lease and records a receipt.
    Never invokes a model, rearms a job or changes execution/cost history.
    """
    return await _recover_completed_raw(
        {
            "mode": "apply",
            "job_name": job_name,
            "session_id": session_id,
            "permit_id": permit_id,
            "expected_state_sha256": expected_state_sha256,
            "reconciliation_key": reconciliation_key,
        }
    )
