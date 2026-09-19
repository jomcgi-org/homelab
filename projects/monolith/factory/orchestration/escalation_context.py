"""Decision-first evidence document for one factory escalation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlmodel import Session, select

from factory import publication
from factory.orchestration.factory_controls import (
    _snapshot,
    escalation_view,
    receipt_task_class,
)
from factory.orchestration.factory_intake import receipts_for_work
from factory.orchestration.factory_models import (
    FactoryReceipt,
    FactoryReviewVerdict,
    WorkItem,
    WorkItemEdge,
)
from factory.orchestration.models import SwarmNodeRun
from factory.orchestration import factory_funding_limits


def _item(item: WorkItem) -> dict:
    return {
        "id": item.id,
        "github_issue_number": item.github_issue_number,
        "title": item.title,
        "state": item.state,
    }


def _reference(item: dict) -> str:
    number = item["github_issue_number"]
    item_id = item["id"]
    if number is not None:
        return f"#{number}"
    else:
        return f"work item {item_id}"


def _lineage(db: Session, receipt: FactoryReceipt) -> dict | None:
    if receipt.work_item_id is None:
        return None
    work_item = db.get(WorkItem, receipt.work_item_id)
    if work_item is None:
        return None

    edges = db.exec(
        select(WorkItemEdge)
        .where(
            or_(
                WorkItemEdge.from_id == work_item.id,
                WorkItemEdge.to_id == work_item.id,
            )
        )
        .order_by(WorkItemEdge.id)
    ).all()
    related_ids = {
        edge.to_id if edge.from_id == work_item.id else edge.from_id for edge in edges
    }
    related = (
        {
            item.id: item
            for item in db.exec(
                select(WorkItem).where(WorkItem.id.in_(sorted(related_ids)))
            ).all()
        }
        if related_ids
        else {}
    )

    def outgoing(kind: str) -> list[dict]:
        return [
            _item(related[edge.to_id])
            for edge in edges
            if edge.from_id == work_item.id
            and edge.kind == kind
            and edge.to_id in related
        ]

    def incoming(kind: str) -> list[dict]:
        return [
            _item(related[edge.from_id])
            for edge in edges
            if edge.to_id == work_item.id
            and edge.kind == kind
            and edge.from_id in related
        ]

    parents = incoming("parent")
    children = outgoing("parent")
    blocked_by = incoming("blocks")
    blocks = outgoing("blocks")
    superseded_by = incoming("supersedes")
    supersedes = outgoing("supersedes")
    prior_rows = sorted(
        (
            row
            for row in receipts_for_work(db, receipt.repo, receipt.issue_number)
            if row.id != receipt.id
        ),
        key=lambda row: (row.created_at, row.id or 0),
    )
    prior_receipts = [
        {
            "id": row.id,
            "generation": row.generation,
            "task_class": receipt_task_class(row),
            "state": row.state,
            "created_at": row.created_at,
        }
        for row in prior_rows
    ]

    claims: list[str] = []
    if parents:
        parent_ref = _reference(parents[0])
        claims.append(f"child of {parent_ref}")
    if children:
        claims.append(f"{len(children)} child" + ("ren" if len(children) != 1 else ""))
    if blocked_by:
        blocked_by_count = len(blocked_by)
        claims.append(
            f"blocked by {blocked_by_count} item"
            + ("s" if blocked_by_count != 1 else "")
        )
    if blocks:
        blocks_count = len(blocks)
        claims.append(
            f"blocks {blocks_count} item" + ("s" if blocks_count != 1 else "")
        )
    if superseded_by:
        claims.append(
            "superseded by " + ", ".join(_reference(item) for item in superseded_by)
        )
    if supersedes:
        claims.append(
            "supersedes " + ", ".join(_reference(item) for item in supersedes)
        )
    if prior_receipts:
        noun = "receipt" if len(prior_receipts) == 1 else "receipts"
        detail = ", ".join(
            f"{row['task_class']}, {row['state']}" for row in prior_receipts
        )
        claims.append(f"{len(prior_receipts)} prior {noun} ({detail})")

    return {
        "line": ", ".join(claims)
        if claims
        else "no related work items or prior receipts",
        "parent": parents[0] if parents else None,
        "children": children,
        "blocked_by": blocked_by,
        "blocks": blocks,
        "superseded_by": superseded_by,
        "supersedes": supersedes,
        "prior_receipts": prior_receipts,
    }


def escalation_context(db: Session, receipt_id: int) -> dict | None:
    """Build five claims and their database evidence without reading GitHub."""
    receipt = db.get(FactoryReceipt, receipt_id)
    if receipt is None:
        return None

    snapshot = _snapshot(db, receipt)
    escalation = escalation_view(snapshot)
    work_item_id = receipt.work_item_id
    ask_line = (
        f"#{receipt.issue_number} {receipt.title}"
        if receipt.issue_number is not None
        else f"work item {work_item_id}"
    )
    ask = {
        "line": ask_line,
        "issue_number": receipt.issue_number,
        "work_item_id": work_item_id,
        "title": receipt.title,
        "url": receipt.url,
        "body_head": " ".join(receipt.body.split())[:400],
        "task_class": receipt_task_class(receipt),
        "generation": receipt.generation,
    }

    stopped = None
    if escalation is not None:
        stopped_line = (
            escalation.get("reason")
            or escalation.get("summary")
            or escalation.get("question")
            or "operator decision requested"
        )
        stopped = {
            "line": stopped_line,
            "kind": escalation["kind"],
            "recommendation": escalation.get("recommendation"),
            "question": escalation.get("question"),
            "summary": escalation.get("summary"),
            "reason": escalation.get("reason"),
            "downgraded": escalation["downgraded"],
            "resolved": escalation.get("resolved") is not None,
            "open": escalation["open"],
        }

    attempts = 0
    last_review = None
    if receipt.task_id is not None:
        attempts = len(
            db.exec(
                select(SwarmNodeRun.id).where(SwarmNodeRun.task_id == receipt.task_id)
            ).all()
        )
        review = db.exec(
            select(FactoryReviewVerdict)
            .where(FactoryReviewVerdict.task_id == receipt.task_id)
            .order_by(
                FactoryReviewVerdict.reviewed_at.desc(),
                FactoryReviewVerdict.id.desc(),
            )
            .limit(1)
        ).first()
        if review is not None:
            last_review = {
                "verdict": review.verdict,
                "summary": review.summary or None,
                "head_sha": review.head_sha,
                "reviewed_at": review.reviewed_at,
            }
    if last_review is None:
        happened_line = (
            "no attempts yet"
            if attempts == 0
            else f"{attempts} attempt" + ("s" if attempts != 1 else "")
        )
    else:
        summary = f": {last_review['summary']}" if last_review["summary"] else ""
        happened_line = (
            f"{attempts} attempt"
            f"{'s' if attempts != 1 else ''}, last review "
            f"{last_review['verdict']}{summary}"
        )
    happened = {
        "line": happened_line,
        "attempts": attempts,
        "last_review": last_review,
        "pr": publication.shape_pr(snapshot.get("evidence")),
        "branch": escalation.get("branch") if escalation is not None else None,
    }

    cost = None
    if receipt.task_id is not None:
        cost_data = factory_funding_limits.objective(db, receipt.task_id)
        if cost_data is not None:
            task_ids = cost_data.get("task_ids", [])
            committed_cost_usd = cost_data.get("committed_cost_usd", 0.0)
            ceiling_usd = cost_data.get("ceiling_usd", 0.0)
            cost = {
                "line": (
                    f"${committed_cost_usd:.2f} of ${ceiling_usd:.2f} objective ceiling "
                    f"across {len(task_ids)} task" + ("s" if len(task_ids) != 1 else "")
                ),
                "committed": committed_cost_usd,
                "ceiling": ceiling_usd,
                "num_tasks": len(task_ids),
            }

    lineage = _lineage(db, receipt)
    lines = [
        ask["line"],
        stopped["line"] if stopped is not None else "no escalation",
        happened["line"],
        cost["line"] if cost is not None else "no task cost",
        lineage["line"] if lineage is not None else "no work item lineage",
    ]
    return {
        "receipt_id": receipt.id,
        "task_id": receipt.task_id,
        "work_item_id": work_item_id,
        "generated_at": datetime.now(timezone.utc),
        "lines": lines,
        "options": (
            [option["label"] for option in escalation["options"]]
            if escalation is not None
            else None
        ),
        "escape": (
            [option["label"] for option in escalation["escape"]]
            if escalation is not None
            else None
        ),
        "ask": ask,
        "stopped": stopped,
        "happened": happened,
        "cost": cost,
        "lineage": lineage,
    }
