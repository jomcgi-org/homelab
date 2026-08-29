"""HTTP surface for the qwen drain console (/api/agents/drain).

Reads live in the database because that is where the drainer's truth lives:
routine_jobs carries the queue and outcomes, the DBOS system tables carry
cycle liveness, and agent_sessions carries per-attempt evidence. The DBOS
reads go straight at the dbos schema tables rather than through the DBOS
client API on purpose: list_workflows needs a constructed instance, which a
follower replica does not launch, and the console is a read-only surface
that must render on any replica. The reaper in drainer_router keeps using
the API because it has to cancel.

Writes: exactly one, requeue, which re-arms a dead one-shot row via the
existing trigger_job primitive (the MCP surface already exposes the same
call). Cancelling a wedged cycle reuses POST /api/swarm/runs/{id}/cancel,
and kicking the lane reuses POST /internal/agent/drain; neither needed a
new endpoint here.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException
from sqlalchemy import bindparam, text
from sqlmodel import Session

from agent.api import list_jobs, load_drainer_settings
from agent_sessions.constants import DRAINER_NODE_KEY
from core.db import get_engine
from core.github import GITHUB_API, GITHUB_REPO
from swarm import drain_console

router = APIRouter(prefix="/api/agents/drain", tags=["agents"])
logger = logging.getLogger(__name__)

# Enough history to cover every job in a large batch plus retries; the join
# only keeps the newest session per job, so overshooting is cheap.
_SESSION_SCAN_LIMIT = 1000
_CYCLE_SCAN_LIMIT = 40
_ACTIVITY_LIST_CAP = 500
_ACTIVITY_TEXT_CAP = 300
_RESULT_HEAD_CHARS = 600
_PR_CACHE_TTL_SECONDS = 3600
# Cache is unbounded by design: grows one small entry per PR number a human actually opens.
_PR_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}


def _github_get(url: str) -> httpx.Response:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response


def _enrich_pr(pr: dict) -> dict:
    repo = pr["repo"]
    number = pr["number"]
    cache_key = (repo, number)
    now = time.monotonic()
    cached = _PR_CACHE.get(cache_key)
    if cached is not None and cached[0] > now:
        return {**pr, **cached[1]}

    try:
        response = _github_get(f"{GITHUB_API}/repos/{repo}/pulls/{number}")
        body = response.json()
        enrichment = {
            key: body[key]
            for key in (
                "title",
                "state",
                "merged",
                "changed_files",
                "additions",
                "deletions",
            )
            if key in body
        }
    except Exception:  # noqa: BLE001
        logger.info("could not enrich GitHub PR %s", number, exc_info=True)
        return pr

    _PR_CACHE[cache_key] = (time.monotonic() + _PR_CACHE_TTL_SECONDS, enrichment)
    return {**pr, **enrichment}


def _server_app_version() -> str:
    """See swarm/router.py _server_app_version: "" means cannot tell."""
    try:
        from dbos._utils import GlobalParams

        return GlobalParams.app_version or ""
    except Exception:  # noqa: BLE001
        logger.warning("could not read the DBOS app version", exc_info=True)
        return ""


def _load_cycles(limit: int = _CYCLE_SCAN_LIMIT) -> list[dict]:
    sql = text(
        """
        SELECT workflow_uuid, status, created_at, updated_at, application_version
          FROM dbos.workflow_status
         WHERE name = 'drain_cycle' AND queue_name = 'drainer'
         ORDER BY created_at DESC
         LIMIT :limit
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"limit": limit}).fetchall()
    return [
        {
            "workflow_uuid": row.workflow_uuid,
            "status": row.status,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "application_version": row.application_version,
        }
        for row in rows
    ]


def _load_step_stats(workflow_ids: list[str]) -> dict[str, dict]:
    """Per-cycle step aggregates from operation_outputs.

    completed_at_epoch_ms over ALL steps is the liveness signal (poll_turn
    checkpoints included, they ARE the heartbeat); the last-step name filters
    the heartbeat out so the rail can say what the cycle was last doing.
    """
    if not workflow_ids:
        return {}
    totals_sql = text(
        """
        SELECT workflow_uuid,
               count(*) AS steps,
               max(completed_at_epoch_ms) AS last_ms,
               count(*) FILTER (WHERE function_name LIKE '%claim_drainer_job%')
                   AS claims,
               count(*) FILTER (WHERE function_name LIKE '%finish_drainer_job%')
                   AS finishes
          FROM dbos.operation_outputs
         WHERE workflow_uuid IN :ids
         GROUP BY workflow_uuid
        """
    ).bindparams(bindparam("ids", expanding=True))
    last_step_sql = text(
        """
        SELECT DISTINCT ON (workflow_uuid) workflow_uuid, function_name
          FROM dbos.operation_outputs
         WHERE workflow_uuid IN :ids
           AND function_name NOT LIKE '%poll_turn%'
           AND function_name NOT LIKE '%DBOS.sleep%'
         ORDER BY workflow_uuid, function_id DESC
        """
    ).bindparams(bindparam("ids", expanding=True))
    with Session(get_engine()) as session:
        totals = session.execute(totals_sql, {"ids": workflow_ids}).fetchall()
        last_steps = session.execute(last_step_sql, {"ids": workflow_ids}).fetchall()
    stats = {
        row.workflow_uuid: {
            "steps": row.steps,
            "last_ms": row.last_ms,
            "claims": row.claims,
            "finishes": row.finishes,
        }
        for row in totals
    }
    for row in last_steps:
        stats.setdefault(row.workflow_uuid, {})["last_step"] = row.function_name
    return stats


def _load_drainer_sessions(limit: int = _SESSION_SCAN_LIMIT) -> list[dict]:
    sql = text(
        """
        SELECT id, local_session_id, workflow_id, status, created_at
          FROM agent_sessions.agent_sessions
         WHERE node_key = :node_key
         ORDER BY id DESC
         LIMIT :limit
        """
    )
    with Session(get_engine()) as session:
        rows = session.execute(
            sql, {"node_key": DRAINER_NODE_KEY, "limit": limit}
        ).fetchall()
    return [
        {
            "id": row.id,
            "local_session_id": row.local_session_id,
            "workflow_id": row.workflow_id,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _load_last_turns(session_ids: list[int]) -> dict[int, dict]:
    """Latest turn stats per session, extracted in SQL.

    usage_json is always json.dumps output or NULL (store.py owns the write),
    so the casts are safe; jsonb_typeof guards the shape anyway because a
    surprise scalar must degrade to NULL rather than abort the query.
    """
    if not session_ids:
        return {}
    sql = text(
        """
        SELECT DISTINCT ON (session_id)
               session_id, seq, terminal_reason, cost_usd, created_at,
               CASE WHEN jsonb_typeof(
                        NULLIF(usage_json, '')::jsonb -> 'activities'
                    ) = 'array'
                    THEN jsonb_array_length(
                        NULLIF(usage_json, '')::jsonb -> 'activities'
                    )
               END AS calls,
               (NULLIF(usage_json, '')::jsonb ->> 'input_tokens') AS input_tokens,
               (NULLIF(usage_json, '')::jsonb ->> 'model_ms') AS model_ms
          FROM agent_sessions.agent_turns
         WHERE session_id IN :ids
         ORDER BY session_id, seq DESC
        """
    ).bindparams(bindparam("ids", expanding=True))
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"ids": session_ids}).fetchall()
    return {
        row.session_id: {
            "seq": row.seq,
            "terminal_reason": row.terminal_reason,
            "cost_usd": row.cost_usd,
            "created_at": row.created_at,
            "calls": row.calls,
            "input_tokens": _int_or_none(row.input_tokens),
            "model_ms": _int_or_none(row.model_ms),
        }
        for row in rows
    }


def _load_partials(session_ids: list[int]) -> dict[int, dict]:
    if not session_ids:
        return {}
    sql = text(
        """
        SELECT session_id, claimed_at,
               CASE WHEN jsonb_typeof(NULLIF(partial_activities, '')::jsonb)
                        = 'array'
                    THEN jsonb_array_length(
                        NULLIF(partial_activities, '')::jsonb
                    )
               END AS live_calls
          FROM agent_sessions.pending_messages
         WHERE session_id IN :ids
        """
    ).bindparams(bindparam("ids", expanding=True))
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"ids": session_ids}).fetchall()
    return {
        row.session_id: {"claimed_at": row.claimed_at, "live_calls": row.live_calls}
        for row in rows
    }


def _load_turns_for_sessions(session_ids: list[int]) -> dict[int, dict]:
    """Full latest turn per session for the job detail view."""
    if not session_ids:
        return {}
    sql = text(
        """
        SELECT DISTINCT ON (session_id)
               session_id, seq, result_text, terminal_reason, cost_usd,
               created_at, usage_json
          FROM agent_sessions.agent_turns
         WHERE session_id IN :ids
         ORDER BY session_id, seq DESC
        """
    ).bindparams(bindparam("ids", expanding=True))
    with Session(get_engine()) as session:
        rows = session.execute(sql, {"ids": session_ids}).fetchall()
    return {row.session_id: row for row in rows}


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _capped_activities(usage_json: str | None) -> tuple[list, int]:
    """The turn's activity list, bounded for transport.

    A runaway turn records hundreds of bash commands; the console wants the
    tail (the loop being repeated) more than the head, so the cap keeps the
    LAST entries. String fields are truncated so one enormous command cannot
    bloat the payload.
    """
    try:
        usage = json.loads(usage_json or "")
    except (TypeError, json.JSONDecodeError):
        return [], 0
    activities = usage.get("activities") if isinstance(usage, dict) else None
    if not isinstance(activities, list):
        return [], 0
    total = len(activities)
    tail = activities[-_ACTIVITY_LIST_CAP:]
    capped = []
    for activity in tail:
        if isinstance(activity, str):
            capped.append(activity[:_ACTIVITY_TEXT_CAP])
        elif isinstance(activity, dict):
            capped.append(
                {
                    key: (
                        value[:_ACTIVITY_TEXT_CAP] if isinstance(value, str) else value
                    )
                    for key, value in activity.items()
                }
            )
        else:
            capped.append(str(activity)[:_ACTIVITY_TEXT_CAP])
    return capped, total


@router.get("/console")
def drain_console_view() -> dict:
    settings = asdict(load_drainer_settings())
    jobs = list_jobs(kind=settings["job_kind"])
    now = datetime.now(timezone.utc)

    sessions = _load_drainer_sessions()
    session_ids = [session["id"] for session in sessions]
    last_turns = _load_last_turns(session_ids)
    running_ids = [s["id"] for s in sessions if s.get("status") == "running"]
    partials = _load_partials(running_ids)
    entries = drain_console.compose_jobs(jobs, sessions, last_turns, partials, now)
    counts = drain_console.queue_counts(entries)

    # The DBOS system tables are a separate concern from the job queue: when
    # they cannot be read the jobs still render and the rail says "unknown"
    # instead of pretending the lane is idle.
    lane_error = None
    cycles: list[dict] = []
    step_stats: dict[str, dict] = {}
    try:
        cycles = _load_cycles()
        step_stats = _load_step_stats([c["workflow_uuid"] for c in cycles])
    except Exception:  # noqa: BLE001
        logger.warning("drain console could not read DBOS tables", exc_info=True)
        lane_error = "cycle state unavailable"

    lane = drain_console.compose_lane(
        cycles,
        step_stats,
        _server_app_version(),
        counts.get("due", 0),
        settings["enabled"],
        now,
        error=lane_error,
    )
    lane["reap_after_seconds"] = drain_console.reap_after_seconds(settings)

    return {
        "enabled": settings["enabled"],
        "kind": settings["job_kind"],
        "settings": {
            "max_jobs_per_cycle": settings["max_jobs_per_cycle"],
            "turn_timeout_seconds": settings["turn_timeout_seconds"],
        },
        "lane": lane,
        "recent_cycles": drain_console.compose_recent_cycles(cycles, step_stats),
        "queue": counts,
        "jobs": entries,
        "now": now.isoformat(),
    }


@router.get("/jobs/{name}")
def drain_job_detail(name: str) -> dict:
    settings = asdict(load_drainer_settings())
    jobs = list_jobs(kind=settings["job_kind"])
    job = next((j for j in jobs if j["name"] == name), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown drain job")
    now = datetime.now(timezone.utc)

    sessions = [
        session
        for session in _load_drainer_sessions()
        if drain_console.job_name_from_session_key(session["local_session_id"]) == name
    ]
    session_ids = [session["id"] for session in sessions]
    turns = _load_turns_for_sessions(session_ids)
    partials = _load_partials(
        [s["id"] for s in sessions if s.get("status") == "running"]
    )

    attempts = []
    for session in sessions:
        turn = turns.get(session["id"])
        activities: list = []
        total_calls = 0
        turn_payload = None
        if turn is not None:
            activities, total_calls = _capped_activities(turn.usage_json)
            turn_payload = {
                "seq": turn.seq,
                "result_head": str(turn.result_text or "")[:_RESULT_HEAD_CHARS],
                "terminal_reason": turn.terminal_reason,
                "cost_usd": turn.cost_usd,
                "created_at": drain_console.iso_dt(turn.created_at),
            }
        partial = partials.get(session["id"])
        attempts.append(
            {
                "session_id": session["id"],
                "workflow_id": session.get("workflow_id"),
                "status": session.get("status"),
                "created_at": drain_console.iso_dt(session.get("created_at")),
                "turn": turn_payload,
                "calls": total_calls,
                "activities": activities,
                "live": (
                    {
                        "calls": partial.get("live_calls"),
                        "claimed_at": drain_console.iso_dt(partial.get("claimed_at")),
                    }
                    if partial is not None
                    else None
                ),
            }
        )

    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    outcome, pr = drain_console.classify_outcome(job)
    if pr is not None:
        pr = _enrich_pr(pr)
    job_payload = {
        "name": job["name"],
        "state": drain_console.job_state(job, now),
        "outcome": outcome,
        "routine_kind": job.get("routine_kind"),
        "prompt": payload.get("prompt"),
        "repo": payload.get("repo"),
        "branch": payload.get("branch"),
        "reasoning": payload.get("reasoning"),
        "last_status": job.get("last_status"),
        "last_summary": job.get("last_summary"),
        "last_run_at": drain_console.iso_dt(job.get("last_run_at")),
        "next_run_at": drain_console.iso_dt(job.get("next_run_at")),
        "locked_by": job.get("locked_by"),
        "locked_at": drain_console.iso_dt(job.get("locked_at")),
        "ttl_secs": job.get("ttl_secs"),
        "created_by": job.get("created_by"),
        "created_at": drain_console.iso_dt(job.get("created_at")),
    }
    if pr is not None:
        job_payload["pr"] = pr
    return {
        "job": job_payload,
        "attempts": attempts,
        "now": now.isoformat(),
    }


@router.post("/jobs/{name}/requeue")
def requeue_drain_job(name: str) -> dict:
    """Re-arm a dead one-shot job so the next cycle claims it again.

    A failed one-shot is otherwise permanently done: complete_job NULLs
    next_run_at whatever the status, so without this the console's failure
    triage dead-ends exactly at the action it exists for. trigger_job is the
    existing primitive (next_run_at = now()); the kind check keeps this
    endpoint from re-arming arbitrary routine jobs, and a live lock refuses
    because re-arming a job that is being worked right now would race its
    own completion.
    """
    from agent import routine_jobs

    settings = asdict(load_drainer_settings())
    jobs = list_jobs(kind=settings["job_kind"])
    job = next((j for j in jobs if j["name"] == name), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown drain job")
    if drain_console.lock_is_live(job, datetime.now(timezone.utc)):
        raise HTTPException(
            status_code=409, detail="Job is running; cancel its cycle first"
        )
    if not routine_jobs.trigger_job(name):
        raise HTTPException(status_code=404, detail="Unknown drain job")
    return {"requeued": True, "name": name}
