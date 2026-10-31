"""Read model for the private agents page's factory board.

One GET builds everything the board renders: the control state and policy,
the receipts split into in-flight, queued and recent, and for each task the
plan nodes joined to their attempts and the agent sessions those attempts ran
in. It is read-only and sits next to /api/agents/sessions on purpose: the
operator-gated /api/swarm/factory routes need a bearer the browser does not
carry on the private tier, and this board is a view, not a control.

The pure shaping helpers take plain dicts so they are testable without a
database; only ``build_factory_view`` touches the engine.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlmodel import Session, select

from core.clock import aware, iso_or_str

RECENT_LIMIT = 12
RESULT_HEAD = 240
ACTIVE_STATES = ("admitted", "uncertain", "landing")
QUEUED_STATES = ("queued",)

# Node run statuses come from factory.orchestration.graph; a node with no run yet is pending
# and a node the planner discarded or cancelled in a later plan revision is
# retired whatever its runs say.
_RUN_STATE = {
    "admitted": "running",
    "succeeded": "done",
    "failed": "failed",
    "escalated": "escalated",
    "uncertain": "uncertain",
    "cancelled": "cancelled",
}


def first_line(text: str | None, limit: int = RESULT_HEAD) -> str:
    """The first non-empty line of a result, clipped for a card."""
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def node_label(node_key: str) -> str:
    """``implement_fix_probe_worker_destroy`` reads as ``implement · fix probe worker destroy``."""
    kind, _, rest = node_key.partition("_")
    return f"{kind} · {rest.replace('_', ' ')}" if rest else kind


def shape_node(
    node: dict,
    runs: list[dict],
    sessions: dict[int, dict],
    *,
    receipt_state: str | None = None,
) -> dict:
    """Join one plan node to its attempts and derive a single display state."""
    attempts = sorted(runs, key=lambda run: run.get("attempt") or 0)
    latest = attempts[-1] if attempts else None
    retired = (
        node.get("discarded_in_version") is not None
        or node.get("cancelled_in_version") is not None
    )
    if retired:
        state = "retired"
    elif latest is None:
        state = "pending"
    else:
        state = _RUN_STATE.get(latest.get("status") or "", "uncertain")
        if state == "running" and receipt_state not in (None, "admitted"):
            state = "uncertain"
        elif state == "running" and receipt_state == "admitted":
            if not (sessions.get(latest.get("session_id")) or {}).get("executing"):
                state = "uncertain"
    shaped_attempts = []
    for run in attempts:
        session_id = run.get("session_id")
        shaped_attempts.append(
            {
                "attempt": run.get("attempt"),
                "status": run.get("status"),
                "model": (run.get("pin") or {}).get("model"),
                "effort": (run.get("pin") or {}).get("effort"),
                "cost_usd": run.get("cost_usd"),
                "accounted_cost_usd": run.get("accounted_cost_usd"),
                "session_id": session_id,
                "created_at": iso_or_str(run.get("created_at")),
                "finished_at": iso_or_str(run.get("finished_at")),
                "session": sessions.get(session_id) if session_id else None,
            }
        )
    return {
        "node_key": node["node_key"],
        "label": node_label(node["node_key"]),
        "kind": node.get("kind"),
        "model": node.get("model"),
        "effort": node.get("effort"),
        "deps": list(node.get("deps") or []),
        "state": state,
        "max_cost_usd": node.get("max_cost_usd"),
        "created_in_version": node.get("created_in_version"),
        "attempts": shaped_attempts,
        "session": shaped_attempts[-1]["session"] if shaped_attempts else None,
    }


BOARD_POLICY_KEYS = (
    "generation",
    "max_tasks",
    "conductor_model",
    "worker_model",
    "reviewer_model",
    "model_pools",
    "role_effort",
    "max_task_turns_hard",
    "max_parallel_nodes",
    "task_budget_usd",
    "max_attempts",
)


def _envelope(policy: dict) -> int | None:
    """The turn envelope, read from the old fixed cap when a policy predates it."""
    ceiling = policy.get("max_task_turns_hard")
    return policy.get("max_turns_per_task") if ceiling is None else ceiling


def shape_policy(policy: dict | None) -> dict | None:
    """The board shows a few things about the policy; send those, not the rest."""
    if not policy:
        return None
    shaped = {key: policy.get(key) for key in BOARD_POLICY_KEYS}
    shaped["max_task_turns_hard"] = _envelope(policy)
    return shaped


def shape_receipt(
    receipt: dict,
    nodes: list[dict] | None = None,
    runs: list[dict] | None = None,
    sessions: dict[int, dict] | None = None,
) -> dict:
    """The board card for one receipt, with its plan when one exists."""
    policy = receipt.get("policy") or {}
    shaped = {
        key: receipt.get(key)
        for key in (
            "id",
            "repo",
            "issue_number",
            "generation",
            "title",
            "url",
            "actor",
            "state",
            "task_id",
            "work_item_id",
            "routing_tier",
            "task_paused",
            "cancellation_requested",
            "created_at",
            "updated_at",
            "admitted_at",
            "deadline_at",
            "turns_used",
            "planner_turns_used",
            "committed_cost_usd",
            "unresolved_starts",
            "limits",
            "evidence",
            "allowance",
            "task_class",
            "escalation",
            # What this issue spent on the attempts before this one. A
            # decision that re-admits an escalated delivery mints a new task,
            # so without these the escalated attempt's cost leaves the board.
            "previous_task_ids",
            "previous_spend",
        )
    }
    shaped["policy"] = {
        key: policy.get(key)
        for key in (
            "conductor_model",
            "worker_model",
            "reviewer_model",
            "max_parallel_nodes",
            "task_budget_usd",
            "max_attempts",
        )
    }
    shaped["policy"]["max_task_turns_hard"] = _envelope(policy)
    shaped["starts"] = [
        {
            "start_key": start.get("start_key"),
            "model": start.get("model"),
            "status": start.get("status"),
            "cost_usd": start.get("cost_usd"),
            "session_id": start.get("session_id"),
        }
        for start in receipt.get("starts") or []
    ]
    by_node: dict[str, list[dict]] = {}
    for run in runs or []:
        by_node.setdefault(run["node_key"], []).append(run)
    shaped["nodes"] = [
        shape_node(
            node,
            by_node.get(node["node_key"], []),
            sessions or {},
            receipt_state=receipt.get("state"),
        )
        for node in nodes or []
    ]
    shaped["stop_events"] = receipt.get("stop_events") or []
    return shaped


def current_execution(row, pending, receipt, turn, now: datetime) -> bool:
    """Fresh worker dispatch evidence, not proof of continuous provider generation.

    A native receipt is prepared immediately before POST. Match the current
    owned head, not an old guest binding. Losing freshness means unknown, never
    cessation; this projection grants no retry or capacity release.
    """
    if (
        pending is None
        or receipt is None
        or row.status != "running"
        or not row.ember_session_id
        or row.result_receipt_fence_id
        or row.guest_cleanup_id
        or not pending.claimed_by_replica
        or pending.claimed_at is None
        or not 0 <= (aware(now) - aware(pending.claimed_at)).total_seconds() <= 30
        or (turn is not None and turn.seq >= pending.seq)
    ):
        return False
    return (
        receipt.session_id == row.id
        and receipt.local_session_id == row.local_session_id
        and receipt.seq == pending.seq
        and receipt.dispatch_count == pending.dispatch_count
        and receipt.claim_owner == pending.claimed_by_replica
        and receipt.guest_id == row.ember_session_id
        and receipt.superseded_at is None
        and receipt.received_at is None
        and receipt.response_observed_at is None
        and receipt.response_observer_released_at is None
    )


def _session_summaries(db: Session, ids: set[int]) -> dict[int, dict]:
    from factory.execution.models import (
        AgentResultReceipt,
        AgentSession,
        AgentTurn,
        PendingMessage,
    )

    if not ids:
        return {}
    rows = db.exec(select(AgentSession).where(AgentSession.id.in_(ids))).all()
    summaries: dict[int, dict] = {}
    for row in rows:
        turn = db.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == row.id)
            .order_by(AgentTurn.seq.desc())
            .limit(1)
        ).first()
        pending = db.exec(
            select(PendingMessage)
            .where(PendingMessage.session_id == row.id)
            .order_by(PendingMessage.seq)
            .limit(1)
        ).first()
        receipt = db.exec(
            select(AgentResultReceipt)
            .where(AgentResultReceipt.session_id == row.id)
            .order_by(AgentResultReceipt.created_at.desc())
            .limit(1)
        ).first()
        summaries[row.id] = {
            "executing": current_execution(
                row, pending, receipt, turn, datetime.now(timezone.utc)
            ),
            "id": row.id,
            "model": row.model,
            "status": row.status,
            "node_key": row.node_key,
            "guest_bound": bool(row.ember_session_id),
            "created_at": iso_or_str(row.created_at),
            "last_turn_at": iso_or_str(row.last_turn_at),
            "turns": turn.seq if turn else 0,
            "cost_usd": turn.cost_usd if turn else None,
            "terminal_reason": turn.terminal_reason if turn else None,
            "result_head": first_line(turn.result_text) if turn else "",
        }
    return summaries


def build_factory_view(
    task_id: str | None = None,
    *,
    session: Session | None = None,
    plan_tasks: Iterable[str] | None = None,
    include_backlog: bool = True,
) -> dict:
    """Everything the factory board needs in one read.

    Plans are loaded for every in-flight task and for ``task_id`` when it
    names a finished one, so opening a recent card costs one more read
    rather than every recent card carrying its whole graph.

    ``plan_tasks`` widens that set without a second pass over the board. The
    public snapshot job (factory/publication.py) needs the plan for
    every task it publishes, and asking for them one board-read at a time
    would re-read the whole control state per task.
    """
    from factory.orchestration.factory_controls import escalations, status

    from core.db import get_engine

    def build(db: Session) -> dict:
        backlog = None
        if include_backlog:
            from factory.orchestration.intake_evidence import backlog as load_backlog

            try:
                with db.begin_nested():
                    backlog = load_backlog(db)
            except Exception:  # noqa: BLE001 - evidence failure is not an empty backlog
                backlog = {
                    "available": False,
                    "issues": [],
                    "coverage": {},
                    "stale": True,
                }
        state = status(session=db)
        if not state.get("ok"):
            return {
                "ok": False,
                "reason": state.get("reason"),
                "state": state.get("state"),
                "policy": None,
                "intake": None,
                "problem_issues": None,
                "backlog_refill": None,
                "lanes": None,
                "review_routing": None,
                "active": [],
                "queued": [],
                "recent": [],
                "escalations": [],
            }
        receipts = state["receipts"]
        active = [r for r in receipts if r["state"] in ACTIVE_STATES]
        queued = [r for r in receipts if r["state"] in QUEUED_STATES]
        recent = [
            r for r in receipts if r["state"] not in ACTIVE_STATES + QUEUED_STATES
        ]
        recent = sorted(recent, key=lambda r: r["id"], reverse=True)[:RECENT_LIMIT]
        with_plan = {r["task_id"] for r in active if r["task_id"]}
        if task_id:
            with_plan.add(task_id)
        with_plan.update(plan_tasks or ())

        def enrich(receipt: dict) -> dict:
            task = receipt.get("task_id")
            if not task or task not in with_plan:
                return shape_receipt(receipt)
            # Imported here, not at module scope: the graph module lives in
            # the swarm package, which the agent_sessions test target does not
            # link, and a board with no plans never needs it.
            from factory.orchestration import graph

            nodes = graph.load_graph(task, session=db)
            runs = graph.node_runs(task, session=db)
            ids = {run["session_id"] for run in runs if run.get("session_id")}
            ids.update(
                start["session_id"]
                for start in receipt.get("starts") or []
                if start.get("session_id")
            )
            return shape_receipt(receipt, nodes, runs, _session_summaries(db, ids))

        return {
            "ok": True,
            "state": state["state"],
            "policy": shape_policy(state["policy"]),
            "intake": state.get("intake"),
            "problem_issues": state.get("problem_issues"),
            "backlog_refill": state.get("backlog_refill"),
            **({"intake_backlog": backlog} if include_backlog else {}),
            "lanes": state.get("lanes"),
            "review_routing": state.get("review_routing"),
            "version": state["version"],
            "actor": state["actor"],
            "control_updated_at": iso_or_str(state.get("control_updated_at")),
            "admitted_count": state["admitted_count"],
            "generated_at": iso_or_str(datetime.now(timezone.utc)),
            "active": [enrich(r) for r in active],
            "queued": [shape_receipt(r) for r in queued],
            "recent": [enrich(r) for r in recent],
            # Escalations come from every receipt, not from the three board
            # buckets: a needs-human refine settles succeeded and drops out of
            # "recent" after twelve more receipts, and the decision it is
            # waiting on does not expire with the card.
            "escalations": escalations(receipts),
        }

    if session is not None:
        return build(session)
    with Session(get_engine()) as db:
        return build(db)
