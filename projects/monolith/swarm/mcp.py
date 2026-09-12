"""MCP tools that read factory conductor state.

Two read-only tools over the composers the operator board already uses, so a
chat session can answer "what is the factory doing, and what needs Joe"
without opening the board. Nothing here writes: every mutation stays on the
authenticated HTTP control surface in ``swarm/factory_router.py``.

The authorization gate matches that router's ``operator`` dependency exactly,
because these tools return the same records it serves. Tool-level entitlement
at the gateway is a separate and coarser control (see
`projects/mcp/ARCHITECTURE.md`), so the floor is enforced here rather than
assumed from the catalogue a caller was shown.

Read shape only. A trimmed row per task keeps a status call answerable in one
message, and the escalation tool carries the detail for the few tasks that
are actually waiting on a person.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from auth.api import Authority, Principal, PrincipalKind, current_principal
from core.mcp_app import mcp

OPERATOR_GROUP = "operators"

# Cloud sessions (claude.ai and Claude Code) hold no factory receipt, so an
# empty active list means "nothing the factory admitted", never "nothing is
# running anywhere". #5786 requires that difference to be visible rather than
# inferred by the reader, and stating it is all this slice can do about it.
COVERAGE = {
    "factory_records": "covered",
    "cloud_sessions": "not_indexed",
}


def _refuse(principal: Principal) -> dict | None:
    """The operator floor, or None when the caller clears it.

    Reports what the caller presented rather than a bare denial. The groups
    claim reaches the monolith through the gateway's token forwarding, so a
    refusal here is usually a missing claim rather than a missing person, and
    that is not something the caller could otherwise tell apart.
    """
    if (
        principal.authority != Authority.STANDING
        or principal.kind != PrincipalKind.HUMAN
        or not principal.has_group(OPERATOR_GROUP)
    ):
        return {
            "ok": False,
            "error": "standing operator authority is required",
            "presented": {
                "authority": str(principal.authority),
                "kind": str(principal.kind),
                "operators_group": principal.has_group(OPERATOR_GROUP),
            },
        }
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress(nodes: list[dict]) -> dict:
    """Where the plan has got to: node states by count, and what runs now."""
    counts: dict[str, int] = {}
    for node in nodes:
        state = node.get("state") or "pending"
        counts[state] = counts.get(state, 0) + 1
    return {
        "counts": counts,
        "running": [
            node.get("node_key") for node in nodes if node.get("state") == "running"
        ],
    }


def _task_row(receipt: dict) -> dict:
    """One task trimmed to what a triage conversation needs.

    ``limits`` keeps only the flags that are actually tripped. All four are
    always present on the board card, and carrying the false ones would bury
    the one that explains why a task stopped advancing.
    """
    allowance = receipt.get("allowance") or {}
    policy = receipt.get("policy") or {}
    escalation = receipt.get("escalation") or {}
    limits = receipt.get("limits") or {}
    return {
        "issue_number": receipt.get("issue_number"),
        "title": receipt.get("title"),
        "url": receipt.get("url"),
        "state": receipt.get("state"),
        "task_class": receipt.get("task_class"),
        "task_id": receipt.get("task_id"),
        "task_paused": receipt.get("task_paused"),
        "cancellation_requested": receipt.get("cancellation_requested"),
        "admitted_at": receipt.get("admitted_at"),
        "deadline_at": receipt.get("deadline_at"),
        "turns_used": receipt.get("turns_used"),
        "allowance_turns": allowance.get("turns"),
        "committed_cost_usd": receipt.get("committed_cost_usd"),
        "task_budget_usd": policy.get("task_budget_usd"),
        "limits_tripped": sorted(key for key, value in limits.items() if value),
        # Non-zero unresolved starts is the wedge that blocks settlement
        # rather than admission, so it reads separately from the limits.
        "unresolved_starts": receipt.get("unresolved_starts"),
        "escalation_open": bool(escalation) and escalation.get("resolved") is None,
        "progress": _progress(receipt.get("nodes") or []),
    }


def _status_payload(include_recent: bool, session=None) -> dict:
    """Compose the board and trim it. Runs in a worker thread.

    ``session`` exists so a test fixture can drive the sync core
    directly. The tool never passes one: a session is not safe across
    the thread boundary, and the composer opens its own.
    """
    from agent_sessions.factory_view import build_factory_view

    view = build_factory_view(session=session)
    if not view.get("ok"):
        return {
            "ok": False,
            "reason": view.get("reason"),
            "state": view.get("state"),
            "observed_at": _now(),
            "coverage": COVERAGE,
        }
    open_escalations = [
        item for item in view.get("escalations") or [] if item.get("open")
    ]
    payload = {
        "ok": True,
        "state": view.get("state"),
        "observed_at": view.get("generated_at") or _now(),
        "version": view.get("version"),
        "actor": view.get("actor"),
        "admitted_count": view.get("admitted_count"),
        "policy": view.get("policy"),
        # Why capacity is idle, when it is: the intake block and today's
        # admissions against the cap, and the per-lane usage beside it.
        "intake": view.get("intake"),
        "lanes": view.get("lanes"),
        "review_routing": view.get("review_routing"),
        "active": [_task_row(receipt) for receipt in view.get("active") or []],
        "queued": [_task_row(receipt) for receipt in view.get("queued") or []],
        "needs_operator": [
            {
                "issue_number": item.get("issue_number"),
                "title": item.get("title"),
                "kind": item.get("kind"),
                "question": item.get("question"),
            }
            for item in open_escalations
        ],
        "coverage": COVERAGE,
    }
    if include_recent:
        payload["recent"] = [_task_row(receipt) for receipt in view.get("recent") or []]
    return payload


def _escalations_payload(include_resolved: bool, session=None) -> dict:
    """Escalation cards straight from the control state. Worker thread.

    Deliberately not composed from the board: the board loads a plan for
    every in-flight task, and an escalation card needs none of it.
    """
    from swarm.factory_controls import escalations, status

    state = status(session=session)
    if not state.get("ok"):
        return {
            "ok": False,
            "reason": state.get("reason"),
            "observed_at": _now(),
            "escalations": [],
        }
    items = escalations(state["receipts"])
    if not include_resolved:
        items = [item for item in items if item.get("open")]
    return {
        "ok": True,
        "observed_at": _now(),
        "open_count": sum(1 for item in items if item.get("open")),
        "escalations": items,
        "coverage": COVERAGE,
    }


@mcp.tool
async def factory_status(include_recent: bool = False) -> dict:
    """Read what the software factory is working on and what is blocked.

    Use this to answer "what is the factory doing", "why is nothing being
    admitted", or "what is stuck" without opening the operator board. It is
    a read: it changes nothing and admits nothing.

    Args:
        include_recent: Also return the most recently settled tasks. Off by
            default, since a triage question is usually about what is running
            now rather than what already finished.

    Returns:
        The control state and policy, the intake block and lane usage that
        explain idle capacity, a trimmed row per active and queued task
        (identity, budget, tripped limits, unresolved starts, and plan
        progress), and the open escalations waiting on a person. Every
        response carries ``observed_at`` and a ``coverage`` map. Cloud
        sessions hold no factory record, so an empty active list is not
        evidence that nothing is running anywhere.
    """
    refusal = _refuse(current_principal())
    if refusal is not None:
        return refusal
    return await asyncio.to_thread(_status_payload, include_recent)


@mcp.tool
async def factory_escalations(include_resolved: bool = False) -> dict:
    """Read the factory escalations waiting on an operator decision.

    An escalation means the lane stopped and wants a person. Each card
    carries the question, the options the brief offered, and any chat rounds
    already exchanged. Recording a decision is not available here: that stays
    on the authenticated control surface.

    Args:
        include_resolved: Also return escalations that already have a
            recorded resolution. Off by default, so the answer is the list of
            things still owed a decision.

    Returns:
        Open escalations first, newest issue first, each with its issue
        number, title, question, recommendation, options, and any linked
        branch or pull request, plus ``observed_at`` and ``open_count``.
    """
    refusal = _refuse(current_principal())
    if refusal is not None:
        return refusal
    return await asyncio.to_thread(_escalations_payload, include_resolved)
