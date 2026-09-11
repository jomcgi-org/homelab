"""Build the public factory snapshot from the private factory tables.

The jomcgi.dev factory pages cannot read the factory. public_reader has no
grant on the swarm or agent_sessions schemas, and the public image is pruned of
the factory code entirely, so there is nothing on that side that could assemble
a board even if the rows were reachable. This module is the private half of the
snapshot pattern that answers that: it reads the real tables with the real
code, shapes three payload kinds, and writes them to public_api tables that
agent_sessions/public_router.py serves with plain SQL.

Three payload kinds, because the three pages differ by an order of magnitude in
size:

* the activity payload is the board (in flight, queued, recent) and is read on
  every visit,
* one task payload is that task's walkthrough (brief, plan nodes, attempts, and
  a digest of every turn),
* one session payload is the full record of one attempt, every turn with its
  prompt, result, diff and rationale.

Prompts, results and diffs go out verbatim: that is the point of the pages, and
the repo owner approved it. Identity does not. ``actor``, ``triggered_by``,
``ember_session_id``, session titles, voice summaries and artifact bodies are
excluded here rather than filtered downstream, so a field has to be added on
purpose to become public.

Every shaping helper takes plain dicts and imports nothing from ``swarm``, so
the unit tests run without a database and without linking the swarm package.
Only ``build_public_snapshot`` and ``write_public_snapshot`` touch the engine.
"""

from __future__ import annotations

import json
import logging
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Integer, String, bindparam
from sqlmodel import Session, select, text

logger = logging.getLogger(__name__)

# One turn's diff, decompressed. 256 KiB is the same ceiling the guest shim
# uses for a progress payload, and it keeps one session payload bounded even
# when a turn rewrote half the repo.
DIFF_LIMIT = 262144
# The shim records one activity per tool call. A long turn can run hundreds;
# the tail is the interesting part, so keep the last 300.
ACTIVITY_LIMIT = 300
BRIEF_PARAGRAPHS = 6
# swarm.factory_controls.DEFAULT_MAX_REVIEW_ROUNDS, duplicated rather than
# imported so this module stays swarm-free for the unit tests. A policy written
# before the cap existed carries no value and the engine assumes this one.
DEFAULT_MAX_REVIEW_ROUNDS = 2

POLICY_KEYS = (
    "generation",
    "max_tasks",
    "conductor_model",
    "worker_model",
    "reviewer_model",
    "max_task_turns_hard",
    "max_parallel_nodes",
    "task_budget_usd",
    "max_attempts",
)

# The engine names its own review rounds correct_<n>; the node's `kind` is
# "work" like any other implementing node, so the round count reads the key.
_CORRECTION_NODE = re.compile(r"^correct_[0-9]+$")

# The board word for a receipt state, matching RECEIPT_STATE_WORD in
# frontend/src/routes/private/agents/factory/factory-view.js so the public
# board and the private board call the same thing by the same name.
RECEIPT_STATE_WORD = {
    "queued": "queued",
    "admitted": "in flight",
    "uncertain": "uncertain",
    "succeeded": "landed",
    "failed": "failed",
    "cancelled": "cancelled",
}
TERMINAL_STATES = ("succeeded", "failed", "cancelled")

# The phase word for a task that never got a node far enough to name one. A
# failed task with no node that ran was escalated out rather than finished, so
# it reads as escalated rather than borrowing the board word.
_PHASE_FALLBACK = {
    "queued": "queued",
    "admitted": "queued",
    "succeeded": "done",
    "uncertain": "escalated",
    "failed": "escalated",
    "cancelled": "cancelled",
}

_ACTIVITY_KEYS = ("type", "file_path", "command", "name")
# The same aliases public_api.agent_activity_daily folds together: different
# providers name the cache-read counter differently.
_CACHE_READ_KEYS = (
    "cache_read_tokens",
    "cache_read_input_tokens",
    "cached_input_tokens",
)

# Stop events carry an actor and a request key on the private board. Neither is
# public; the public page shows what happened and whether a human was needed.
_STOP_EVENT_KEYS = ("action", "reason", "intervention_required")


def _iso(value) -> str | None:
    """ISO 8601 in UTC, treating a naive timestamp as UTC like the board does."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def flatten_max_tasks(value: object) -> int | None:
    """The delivery lane's concurrency as one integer.

    A stored policy carries ``max_tasks`` either as a bare integer (older
    policies) or as a per-lane dict ``{"delivery": n, "advisory": m}``
    (``swarm.factory_controls._validate_max_tasks``). The public pages talk
    about delivery tasks only, so the dict collapses to its delivery count.
    """
    if isinstance(value, dict):
        value = value.get("delivery")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def shape_policy(board_policy: dict | None, raw_policy: dict | None = None) -> dict:
    """The board's policy block plus the review-round cap the board omits.

    ``board_policy`` is what ``factory_view.shape_policy`` already resolved (it
    folds the legacy ``max_turns_per_task`` into ``max_task_turns_hard``);
    ``raw_policy`` is the stored control policy, read only for the cap.
    """
    shaped = {key: (board_policy or {}).get(key) for key in POLICY_KEYS}
    shaped["max_tasks"] = flatten_max_tasks(shaped.get("max_tasks"))
    rounds = (raw_policy or {}).get("max_review_rounds")
    shaped["max_review_rounds"] = (
        DEFAULT_MAX_REVIEW_ROUNDS if rounds is None else rounds
    )
    return shaped


def shape_pr(evidence: dict | None) -> dict | None:
    """The delivery's pull request, recovered from the finish_task evidence."""
    url = (evidence or {}).get("pr_url")
    if not url:
        return None
    tail = str(url).rstrip("/").rsplit("/", 1)[-1]
    return {
        "number": int(tail) if tail.isdigit() else None,
        "url": str(url),
        "state": (evidence or {}).get("state"),
    }


def shape_brief(body: str | None) -> list[str]:
    """The issue body as paragraphs, bounded so a long issue cannot dominate."""
    if not body:
        return []
    paragraphs = [
        block.strip()
        for block in re.split(r"\n[ \t]*\n", str(body).replace("\r\n", "\n"))
    ]
    return [block for block in paragraphs if block][:BRIEF_PARAGRAPHS]


def shape_stop_events(events: list[dict] | None) -> list[dict]:
    """Lifecycle stop events with the actor and the request key removed."""
    shaped = []
    for event in events or []:
        shaped.append(
            {
                **{key: event.get(key) for key in _STOP_EVENT_KEYS},
                "at": _iso(event.get("created_at")),
            }
        )
    return shaped


def latest_attempt_finish(nodes: list[dict] | None) -> str | None:
    """The last attempt to finish anywhere in the plan."""
    finishes = [
        attempt.get("finished_at")
        for node in nodes or []
        for attempt in node.get("attempts") or []
        if attempt.get("finished_at")
    ]
    return max(finishes) if finishes else None


def task_phase(receipt: dict) -> str:
    """The node the task is on: what is running, else what ran last."""
    nodes = receipt.get("nodes") or []
    running = next((node for node in nodes if node.get("state") == "running"), None)
    if running:
        return running["node_key"]
    started = [
        (
            max(
                attempt.get("created_at") or ""
                for attempt in node.get("attempts") or []
            ),
            node.get("node_key"),
        )
        for node in nodes
        if node.get("attempts")
    ]
    if started:
        return max(started)[1]
    return _PHASE_FALLBACK.get(receipt.get("state"), "queued")


def review_rounds(nodes: list[dict] | None) -> int:
    """How many engine-owned correction rounds this task has opened."""
    return sum(
        1
        for node in nodes or []
        if _CORRECTION_NODE.fullmatch(node.get("node_key") or "")
    )


def task_summary(receipt: dict) -> dict:
    """One board card: the task's identity, budget, and where it has got to."""
    nodes = receipt.get("nodes") or []
    state = receipt.get("state")
    allowance = receipt.get("allowance") or {}
    return {
        "issue_number": receipt.get("issue_number"),
        "generation": receipt.get("generation"),
        "title": receipt.get("title"),
        "url": receipt.get("url"),
        "state": RECEIPT_STATE_WORD.get(state, state),
        "phase": task_phase(receipt),
        "task_class": receipt.get("task_class"),
        "admitted_at": _iso(receipt.get("admitted_at")),
        "deadline_at": _iso(receipt.get("deadline_at")),
        "finished_at": (
            latest_attempt_finish(nodes) if state in TERMINAL_STATES else None
        ),
        "turns_used": receipt.get("turns_used"),
        "planner_turns_used": receipt.get("planner_turns_used"),
        "allowance_turns": allowance.get("turns"),
        "committed_cost_usd": _float(receipt.get("committed_cost_usd")),
        "review_rounds": review_rounds(nodes),
        "pr": shape_pr(receipt.get("evidence")),
        "nodes": [
            {"node_key": node.get("node_key"), "state": node.get("state")}
            for node in nodes
        ],
    }


def load_usage(usage_json: str | None) -> dict | None:
    """``usage_json`` is stored as a JSON string; a bad one is not a failure."""
    if not usage_json:
        return None
    try:
        parsed = json.loads(usage_json)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def shape_activities(usage: dict | None) -> list[dict]:
    """The tool calls the guest shim recorded, tail-bounded and key-filtered."""
    items = (usage or {}).get("activities")
    if not isinstance(items, list):
        return []
    shaped = []
    for item in items[-ACTIVITY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        shaped.append({key: item[key] for key in _ACTIVITY_KEYS if key in item})
    return shaped


def shape_usage(usage: dict | None) -> dict | None:
    """Token counts only. Null when the turn recorded no usable numbers."""
    if not isinstance(usage, dict):
        return None
    cache_read = next(
        (usage[key] for key in _CACHE_READ_KEYS if usage.get(key) is not None), None
    )
    values = {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "cache_read_tokens": _int(cache_read),
    }
    return None if all(value is None for value in values.values()) else values


def decode_diff(blob: bytes | None, truncated: bool | None) -> tuple[str | None, bool]:
    """Decompress one turn's diff, capping the decoded text at DIFF_LIMIT.

    The stored flag already says the capture itself was cut short; cutting here
    sets it too, so a reader never mistakes a clipped diff for a whole one.
    """
    if not blob:
        return None, bool(truncated)
    # Bounded decompress, the transport.py idiom: ask for one byte past the cap
    # so a blob that decodes to more than DIFF_LIMIT is detected without ever
    # materialising the rest of it.
    try:
        raw = zlib.decompressobj().decompress(blob, DIFF_LIMIT + 1)
    except (zlib.error, TypeError):
        logger.warning("factory_public.diff_undecodable")
        return None, True
    if len(raw) > DIFF_LIMIT:
        return raw[:DIFF_LIMIT].decode("utf-8", "replace"), True
    return raw.decode("utf-8", "replace"), bool(truncated)


def shape_rationale(result_text: str | None) -> dict | None:
    """The rationale trailer, wrapped to the two fields the page shows.

    There is no rationale column: it is parsed out of the result at render
    time. ``paths`` and ``deviations`` stay private, so the public page gets
    the raw trailer and whether it parsed.
    """
    from agent_sessions.rationale import parse_rationale

    parsed = parse_rationale(result_text)
    if parsed.get("parse_status") == "none":
        return None
    return {"raw": parsed.get("raw"), "parse_status": parsed.get("parse_status")}


def shape_permission_denials(value) -> list:
    """The denial list, stored as a JSON string. Anything else reads as none."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def turn_digest(turn: dict) -> dict:
    """What a turn looks like inside a task walkthrough: no diff, no rationale."""
    usage = load_usage(turn.get("usage_json"))
    return {
        "seq": turn.get("seq"),
        "prompt": turn.get("prompt"),
        "activities": shape_activities(usage),
        "result_text": turn.get("result_text"),
        "cost_usd": _float(turn.get("cost_usd")),
        "commit_sha": turn.get("commit_sha"),
    }


def turn_record(turn: dict) -> dict:
    """One turn in full: the digest plus the diff, rationale and usage."""
    usage = load_usage(turn.get("usage_json"))
    diff, truncated = decode_diff(turn.get("diff_blob"), turn.get("diff_truncated"))
    return {
        **turn_digest(turn),
        "base_sha": turn.get("base_sha") or turn.get("diff_base_sha"),
        "diff": diff,
        "diff_truncated": truncated,
        "rationale": shape_rationale(turn.get("result_text")),
        "usage": shape_usage(usage),
        "stop_reason": turn.get("stop_reason"),
        "terminal_reason": turn.get("terminal_reason"),
        "permission_denials": shape_permission_denials(turn.get("permission_denials")),
        "created_at": _iso(turn.get("created_at")),
    }


def shape_node(
    node: dict, session_keys: dict[int, str], turns: dict[int, list]
) -> dict:
    """One plan node with its attempts, each attempt carrying its turn digests."""
    attempts = []
    for attempt in node.get("attempts") or []:
        session_id = attempt.get("session_id")
        attempts.append(
            {
                "attempt": attempt.get("attempt"),
                "status": attempt.get("status"),
                "cost_usd": _float(attempt.get("cost_usd")),
                "session_key": session_keys.get(session_id),
                "created_at": _iso(attempt.get("created_at")),
                "finished_at": _iso(attempt.get("finished_at")),
                "turns": [turn_digest(turn) for turn in turns.get(session_id) or []],
            }
        )
    return {
        "node_key": node.get("node_key"),
        "label": node.get("label"),
        "kind": node.get("kind"),
        "model": node.get("model"),
        "deps": list(node.get("deps") or []),
        "state": node.get("state"),
        "attempts": attempts,
    }


def task_payload(
    receipt: dict,
    policy: dict,
    body: str | None,
    session_keys: dict[int, str],
    turns: dict[int, list],
    snapshotted_at: str,
) -> dict:
    """One task's walkthrough page."""
    return {
        "snapshotted_at": snapshotted_at,
        "policy": policy,
        "task": {
            **task_summary(receipt),
            "brief": shape_brief(body),
            "evidence_reason": (receipt.get("evidence") or {}).get("reason"),
            "nodes": [
                shape_node(node, session_keys, turns)
                for node in receipt.get("nodes") or []
            ],
            "stop_events": shape_stop_events(receipt.get("stop_events")),
        },
    }


def session_payload(
    row: dict,
    issue_number: int | None,
    node_key: str | None,
    attempt: int | None,
    turns: list[dict],
    snapshotted_at: str,
) -> dict:
    """One attempt's full record."""
    costs = [_float(turn.get("cost_usd")) for turn in turns]
    known = [cost for cost in costs if cost is not None]
    last = turns[-1] if turns else {}
    return {
        "snapshotted_at": snapshotted_at,
        "session": {
            "key": row.get("local_session_id"),
            "issue_number": issue_number,
            "node_key": node_key,
            "attempt": attempt,
            "model": row.get("model"),
            "status": row.get("status"),
            "guest_bound": bool(row.get("ember_session_id")),
            "created_at": _iso(row.get("created_at")),
            "last_turn_at": _iso(row.get("last_turn_at")),
            "terminal_reason": last.get("terminal_reason"),
            "turn_count": len(turns),
            "cost_usd": sum(known) if known else None,
        },
        "turns": [turn_record(turn) for turn in turns],
    }


@dataclass(frozen=True)
class PublicSnapshot:
    """The three payload kinds, keyed the way the snapshot tables key them."""

    activity: dict
    tasks: dict[int, dict]
    sessions: dict[str, dict]
    session_issues: dict[str, int | None]


def _board_task_ids(db: Session) -> tuple[set[str], dict[int, str]]:
    """The task ids the board will show, and every receipt's issue body.

    ``build_factory_view`` decides in flight / queued / recent itself, so this
    mirrors its split rather than inventing one: passing a task id that turns
    out not to be on the board only costs a plan read that nothing renders.
    """
    from swarm.factory_models import FactoryReceipt

    from agent_sessions.factory_view import ACTIVE_STATES, QUEUED_STATES, RECENT_LIMIT

    rows = db.exec(
        select(
            FactoryReceipt.id,
            FactoryReceipt.state,
            FactoryReceipt.task_id,
            FactoryReceipt.body,
        ).order_by(FactoryReceipt.id)
    ).all()
    live = [row for row in rows if row.state in ACTIVE_STATES]
    recent = sorted(
        (row for row in rows if row.state not in ACTIVE_STATES + QUEUED_STATES),
        key=lambda row: row.id,
        reverse=True,
    )[:RECENT_LIMIT]
    task_ids = {row.task_id for row in (*live, *recent) if row.task_id}
    return task_ids, {row.id: row.body for row in rows}


def _raw_policy(db: Session) -> dict:
    """The stored control policy, read for the fields the board view drops."""
    from swarm.factory_models import FactoryControl

    control = db.exec(
        select(FactoryControl).where(FactoryControl.id == "factory")
    ).first()
    if control is None or not control.policy_json:
        return {}
    try:
        parsed = json.loads(control.policy_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_TURN_COLUMNS = (
    "seq",
    "prompt",
    "result_text",
    "terminal_reason",
    "stop_reason",
    "permission_denials",
    "commit_sha",
    "base_sha",
    "diff_blob",
    "diff_truncated",
    "diff_base_sha",
    "usage_json",
    "cost_usd",
    "created_at",
)
_SESSION_COLUMNS = (
    "id",
    "local_session_id",
    "model",
    "status",
    "ember_session_id",
    "created_at",
    "last_turn_at",
)


def _sessions_and_turns(
    db: Session, ids: set[int]
) -> tuple[dict[int, dict], dict[int, list[dict]]]:
    """Every referenced session row and its turns, in two reads rather than 2N."""
    from agent_sessions.models import AgentSession, AgentTurn

    if not ids:
        return {}, {}
    sessions = {
        row.id: {column: getattr(row, column) for column in _SESSION_COLUMNS}
        for row in db.exec(select(AgentSession).where(AgentSession.id.in_(ids))).all()
    }
    turns: dict[int, list[dict]] = {}
    for row in db.exec(
        select(AgentTurn)
        .where(AgentTurn.session_id.in_(ids))
        .order_by(AgentTurn.session_id, AgentTurn.seq)
    ).all():
        turns.setdefault(row.session_id, []).append(
            {column: getattr(row, column) for column in _TURN_COLUMNS}
        )
    return sessions, turns


def build_public_snapshot(session: Session) -> PublicSnapshot:
    """Read the factory once and shape everything the public pages serve."""
    from agent_sessions.factory_view import build_factory_view

    snapshotted_at = _iso(datetime.now(timezone.utc))
    task_ids, bodies = _board_task_ids(session)
    board = build_factory_view(session=session, plan_tasks=task_ids)
    policy = shape_policy(board.get("policy"), _raw_policy(session))

    if not board.get("ok"):
        return PublicSnapshot(
            activity={
                "snapshotted_at": snapshotted_at,
                "state": board.get("state"),
                "policy": policy,
                "active": [],
                "queued": [],
                "recent": [],
            },
            tasks={},
            sessions={},
            session_issues={},
        )

    cards = [
        *(board.get("active") or []),
        *(board.get("queued") or []),
        *(board.get("recent") or []),
    ]
    session_ids = {
        attempt["session_id"]
        for card in cards
        for node in card.get("nodes") or []
        for attempt in node.get("attempts") or []
        if attempt.get("session_id")
    }
    session_rows, turns = _sessions_and_turns(session, session_ids)
    session_keys = {sid: row["local_session_id"] for sid, row in session_rows.items()}

    activity = {
        "snapshotted_at": snapshotted_at,
        "state": board.get("state"),
        "policy": policy,
        "active": [task_summary(card) for card in board.get("active") or []],
        "queued": [task_summary(card) for card in board.get("queued") or []],
        "recent": [task_summary(card) for card in board.get("recent") or []],
    }

    tasks: dict[int, dict] = {}
    sessions: dict[str, dict] = {}
    session_issues: dict[str, int | None] = {}
    for card in cards:
        issue_number = card.get("issue_number")
        if issue_number is None:
            continue
        tasks[issue_number] = task_payload(
            card,
            policy,
            bodies.get(card.get("id")),
            session_keys,
            turns,
            snapshotted_at,
        )
        for node in card.get("nodes") or []:
            for attempt in node.get("attempts") or []:
                sid = attempt.get("session_id")
                row = session_rows.get(sid)
                if row is None:
                    continue
                key = row["local_session_id"]
                sessions[key] = session_payload(
                    row,
                    issue_number,
                    node.get("node_key"),
                    attempt.get("attempt"),
                    turns.get(sid) or [],
                    snapshotted_at,
                )
                session_issues[key] = issue_number

    return PublicSnapshot(
        activity=activity,
        tasks=tasks,
        sessions=sessions,
        session_issues=session_issues,
    )


_ACTIVITY_UPSERT = text(
    """
    INSERT INTO public_api.factory_activity_snapshot (id, payload, snapshotted_at)
    VALUES (1, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (id) DO UPDATE
    SET payload = EXCLUDED.payload, snapshotted_at = EXCLUDED.snapshotted_at
    """
)
_TASK_UPSERT = text(
    """
    INSERT INTO public_api.factory_task_snapshot
        (issue_number, payload, snapshotted_at)
    VALUES (:issue_number, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (issue_number) DO UPDATE
    SET payload = EXCLUDED.payload, snapshotted_at = EXCLUDED.snapshotted_at
    """
)
_SESSION_UPSERT = text(
    """
    INSERT INTO public_api.factory_session_snapshot
        (session_key, issue_number, payload, snapshotted_at)
    VALUES (:session_key, :issue_number, CAST(:payload AS jsonb), :snapshotted_at)
    ON CONFLICT (session_key) DO UPDATE
    SET issue_number = EXCLUDED.issue_number,
        payload = EXCLUDED.payload,
        snapshotted_at = EXCLUDED.snapshotted_at
    """
)
# The board is a rolling window, so a task that falls off the recent list stops
# being published. Without these the tables would grow without bound.
# The expanding bindparams carry an explicit type: with an EMPTY keep-list
# SQLAlchemy renders the placeholder as CAST(NULL AS <type>), and an untyped
# one defaults to INTEGER, which Postgres refuses against the TEXT session_key.
_TASK_PRUNE = text(
    "DELETE FROM public_api.factory_task_snapshot WHERE issue_number NOT IN :keep"
).bindparams(bindparam("keep", expanding=True, type_=Integer()))
_SESSION_PRUNE = text(
    "DELETE FROM public_api.factory_session_snapshot WHERE session_key NOT IN :keep"
).bindparams(bindparam("keep", expanding=True, type_=String()))


def _encode(payload: dict) -> str:
    """JSON for a jsonb parameter, with non-ASCII sent as UTF-8 bytes.

    ``ensure_ascii`` would emit ``\\u00b7`` escapes for the middle dots in node
    labels, and a server whose encoding is SQL_ASCII (the CI database) refuses
    those inside jsonb. Raw UTF-8 is accepted by both that server and the UTF-8
    production cluster.
    """
    return json.dumps(payload, ensure_ascii=False)


def write_public_snapshot(session: Session) -> dict:
    """Build the snapshot and replace what the public tables hold with it."""
    snapshot = build_public_snapshot(session)
    at = snapshot.activity["snapshotted_at"]

    session.execute(
        _ACTIVITY_UPSERT,
        {"payload": _encode(snapshot.activity), "snapshotted_at": at},
    )
    for issue_number, payload in snapshot.tasks.items():
        session.execute(
            _TASK_UPSERT,
            {
                "issue_number": issue_number,
                "payload": _encode(payload),
                "snapshotted_at": at,
            },
        )
    for key, payload in snapshot.sessions.items():
        session.execute(
            _SESSION_UPSERT,
            {
                "session_key": key,
                "issue_number": snapshot.session_issues.get(key),
                "payload": _encode(payload),
                "snapshotted_at": at,
            },
        )
    session.execute(_TASK_PRUNE, {"keep": list(snapshot.tasks)})
    session.execute(_SESSION_PRUNE, {"keep": list(snapshot.sessions)})
    session.commit()

    return {
        "tasks": len(snapshot.tasks),
        "sessions": len(snapshot.sessions),
        "snapshotted_at": at,
    }
