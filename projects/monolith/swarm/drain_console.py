"""Server-owned composition for the qwen drain console.

Like swarm/view.py, this module knows nothing about FastAPI or the database.
The router reduces routine_jobs rows, DBOS system-table rows, and agent
session rows to plain dicts at the edge; everything here is pure functions
over those dicts, so the browser never infers lane state and the tests never
need Postgres.

The truth model, and why the cycle is a health rail rather than the spine:
the job is what Joe queues and what carries the outcome (last_status and
last_summary), the session is one attempt's evidence and hangs off the job,
and the cycle is plumbing with exactly one interesting property, liveness.
There is at most one live cycle (the drainer queue has concurrency 1), so it
renders as a single strip above the job list instead of as a collection.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from agent_sessions.constants import DRAINER_NODE_KEY

# Lane liveness thresholds, in seconds of checkpoint silence.
#
# A healthy cycle checkpoints poll_turn every 5 seconds while it awaits a
# turn, so any live cycle should show single-digit silence. The silent
# stretches that are still survivable happen inside non-poll steps: a
# control-plane session create has legitimately blocked for around three
# minutes before failing over. QUIET flags that degraded-but-maybe-alive
# band; WEDGED means nothing healthy has ever been silent this long, and it
# still fires far before the trigger endpoint's reaper (turn_timeout plus
# per-job margin, tens of minutes), so the console names the wedge while the
# automation is still waiting it out.
QUIET_AFTER_SECONDS = 120
WEDGED_AFTER_SECONDS = 600

# The session key format the drainer mints: <workflow_id>:qwen-drain:<job>.
_SESSION_KEY_MARKER = f":{DRAINER_NODE_KEY}:"

PROMPT_HEAD_CHARS = 160
SUMMARY_HEAD_CHARS = 200

_PR_URL_RE = re.compile(
    r"(?<![\w.-])(?P<url>https://github\.com/"
    r"(?P<repo>[^/\s]+/[^/\s]+)/pull/(?P<number>\d+))",
    re.IGNORECASE,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_dt(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat() if value is not None else None


def iso_from_ms(value: int | None) -> str | None:
    """DBOS system-table timestamps are bigint epoch milliseconds."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def job_name_from_session_key(local_session_id: str | None) -> str | None:
    """The job name a drainer session was minted for, or None.

    The workflow id half is not parsed out because job name is the join key
    the console needs; the workflow id is carried on the session row itself.
    """
    if not local_session_id or _SESSION_KEY_MARKER not in local_session_id:
        return None
    return local_session_id.split(_SESSION_KEY_MARKER, 1)[1] or None


def _head(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text.split("\n")[0][:limit]


def classify_outcome(job: dict) -> tuple[str, dict | None]:
    """Classify a persisted job result and parse a linked pull request."""
    if job.get("last_status") == "error":
        return "error", None
    match = _PR_URL_RE.search(str(job.get("last_summary") or ""))
    if match is None:
        return "report", None
    return (
        "pr",
        {
            "url": match.group("url"),
            "number": int(match.group("number")),
            "repo": match.group("repo"),
        },
    )


def lock_is_live(job: dict, now: datetime) -> bool:
    locked_at = _as_utc(job.get("locked_at"))
    return (
        job.get("locked_by") is not None
        and locked_at is not None
        and locked_at + timedelta(seconds=job.get("ttl_secs") or 0) > now
    )


def job_state(job: dict, now: datetime) -> str:
    """One word per job, mirroring the claim predicate in routine_jobs.

    running wins over due on purpose: a claimed job keeps its next_run_at
    until complete_job clears it, so a live lock is the only thing that
    distinguishes "being worked" from "waiting to be claimed". An expired
    lock deliberately falls through to due, because that is exactly when the
    row is reclaimable.
    """
    if lock_is_live(job, now):
        return "running"
    next_run_at = _as_utc(job.get("next_run_at"))
    if next_run_at is not None:
        return "due" if next_run_at <= now else "scheduled"
    if job.get("last_run_at") is not None:
        return "error" if job.get("last_status") == "error" else "ok"
    # Registered but never armed and never run: parked until trigger_job.
    return "parked"


_STATE_ORDER = {
    "running": 0,
    "due": 1,
    "scheduled": 2,
    "error": 3,
    "ok": 3,
    "parked": 4,
}


def _job_sort_key(entry: dict) -> tuple:
    state = entry["state"]
    next_run_at = entry.get("next_run_at") or ""
    last_run_at = entry.get("last_run_at") or ""
    # Waiting work drains oldest-first (the claim order). Finished work (ok and
    # error together) reads newest-first as one linear history.
    if state in ("due", "scheduled"):
        return (_STATE_ORDER[state], 0, next_run_at, entry["name"])
    return (_STATE_ORDER.get(state, 9), 1, _invert(last_run_at), entry["name"])


def _invert(value: str) -> str:
    # Descending sort for an ISO timestamp inside an ascending tuple sort.
    return "".join(chr(0x10FFFF - ord(ch)) for ch in value)


def compose_jobs(
    jobs: list[dict],
    sessions: list[dict],
    last_turns: dict[int, dict],
    partials: dict[int, dict],
    now: datetime,
) -> list[dict]:
    """Join routine jobs to their latest drainer session and its evidence.

    sessions arrive newest-first (the loader orders by id desc), so the first
    session seen per job is the latest attempt and the rest only count.
    """
    latest: dict[str, dict] = {}
    attempts: dict[str, int] = {}
    for session in sessions:
        name = job_name_from_session_key(session.get("local_session_id"))
        if name is None:
            continue
        attempts[name] = attempts.get(name, 0) + 1
        latest.setdefault(name, session)

    entries = []
    for job in jobs:
        name = job["name"]
        outcome, pr = classify_outcome(job)
        session = latest.get(name)
        session_payload = None
        if session is not None:
            turn = last_turns.get(session["id"]) or {}
            partial = partials.get(session["id"]) or {}
            session_payload = {
                "id": session["id"],
                "status": session.get("status"),
                "workflow_id": session.get("workflow_id"),
                "created_at": iso_dt(session.get("created_at")),
                "terminal_reason": turn.get("terminal_reason"),
                "calls": turn.get("calls"),
                "cost_usd": turn.get("cost_usd"),
                "input_tokens": turn.get("input_tokens"),
                "model_ms": turn.get("model_ms"),
                "live_calls": partial.get("live_calls"),
                "claimed_at": iso_dt(partial.get("claimed_at")),
                "attempts": attempts.get(name, 0),
            }
        entry = {
            "name": name,
            "state": job_state(job, now),
            "outcome": outcome,
            "prompt_head": _head(
                (job.get("payload") or {}).get("prompt")
                if isinstance(job.get("payload"), dict)
                else "",
                PROMPT_HEAD_CHARS,
            ),
            "summary_head": _head(job.get("last_summary"), SUMMARY_HEAD_CHARS),
            "last_status": job.get("last_status"),
            "last_run_at": iso_dt(job.get("last_run_at")),
            "next_run_at": iso_dt(job.get("next_run_at")),
            "locked_at": iso_dt(job.get("locked_at")),
            "session": session_payload,
        }
        if pr is not None:
            entry["pr"] = pr
        entries.append(entry)
    entries.sort(key=_job_sort_key)
    return entries


def queue_counts(entries: list[dict]) -> dict:
    counts = {state: 0 for state in _STATE_ORDER}
    for entry in entries:
        state = entry["state"]
        counts[state] = counts.get(state, 0) + 1
    return counts


def compose_lane(
    cycles: list[dict],
    step_stats: dict[str, dict],
    server_app_version: str,
    due_count: int,
    enabled: bool,
    now: datetime,
    error: str | None = None,
) -> dict:
    """The health rail: one live cycle reduced to a state word plus evidence.

    Liveness comes from step checkpoints (operation_outputs), never from
    workflow_status.updated_at alone: updated_at moves only on status
    transitions, so it is frozen at dequeue for the whole life of a healthy
    PENDING cycle. A fresh dequeue has no steps yet, which is why updated_at
    still participates as the floor.
    """
    if error is not None:
        return {"state": "unknown", "cycle": None, "error": error}

    live = next((c for c in cycles if c.get("status") in ("PENDING", "ENQUEUED")), None)
    if live is None:
        if not enabled:
            return {"state": "off", "cycle": None, "error": None}
        return {
            "state": "waiting" if due_count > 0 else "idle",
            "cycle": None,
            "error": None,
        }

    stats = step_stats.get(live["workflow_uuid"]) or {}
    last_ms = max(
        stats.get("last_ms") or 0,
        live.get("updated_at") or 0,
        live.get("created_at") or 0,
    )
    checkpoint_age = None
    if last_ms:
        checkpoint_age = max(0, int(now.timestamp() - last_ms / 1000))

    status = live.get("status")
    if status == "ENQUEUED":
        version = live.get("application_version")
        stranded = bool(
            server_app_version and version and version != server_app_version
        )
        state = "stranded" if stranded else "waiting"
    elif checkpoint_age is None or checkpoint_age >= WEDGED_AFTER_SECONDS:
        state = "wedged"
    elif checkpoint_age >= QUIET_AFTER_SECONDS:
        state = "quiet"
    else:
        state = "running"

    created_ms = live.get("created_at") or 0
    return {
        "state": state,
        "cycle": {
            "workflow_id": live["workflow_uuid"],
            "status": status,
            "created_at": iso_from_ms(created_ms or None),
            "age_seconds": (
                max(0, int(now.timestamp() - created_ms / 1000)) if created_ms else None
            ),
            "last_checkpoint_at": iso_from_ms(last_ms or None),
            "checkpoint_age_seconds": checkpoint_age,
            "last_step": stats.get("last_step"),
            "steps": stats.get("steps") or 0,
            "claims": stats.get("claims") or 0,
            "finishes": stats.get("finishes") or 0,
            "application_version": live.get("application_version"),
        },
        "error": None,
    }


def compose_recent_cycles(
    cycles: list[dict], step_stats: dict[str, dict], limit: int = 8
) -> list[dict]:
    recent = []
    for cycle in cycles:
        if cycle.get("status") in ("PENDING", "ENQUEUED"):
            continue
        stats = step_stats.get(cycle["workflow_uuid"]) or {}
        created = cycle.get("created_at") or 0
        updated = cycle.get("updated_at") or 0
        duration = int((updated - created) / 1000) if created and updated else None
        recent.append(
            {
                "workflow_id": cycle["workflow_uuid"],
                "status": cycle.get("status"),
                "created_at": iso_from_ms(created or None),
                "duration_seconds": duration,
                "finishes": stats.get("finishes") or 0,
            }
        )
        if len(recent) >= limit:
            break
    return recent


def reap_after_seconds(settings: dict) -> int:
    """Mirror of the trigger endpoint's staleness threshold, for display.

    Shown on the rail so a wedged cycle also says when the automation would
    reap it unaided. Keep in agreement with _reap_stale_drain_cycles in
    swarm/drainer_router.py (margin 600).
    """
    return (
        int(settings["turn_timeout_seconds"])
        + int(settings["max_jobs_per_cycle"]) * 60
        + 600
    )
