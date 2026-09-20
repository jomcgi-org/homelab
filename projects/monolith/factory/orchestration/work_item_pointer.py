"""Synchronize concise GitHub pointers to durable factory work items.

The pointer sync happens in three phases to avoid holding the exclusive factory
control lock during GitHub writes (which have a 15 second timeout). A 20-item
pass with 15s GitHub latency would otherwise pin the lock for minutes, stalling
receive_issue, admit_next, landing audits and the operator endpoints.

Phase 1: Under lock, select candidate rows and collect their render inputs.
Phase 2: No lock, GitHub writes (POST or PATCH for each candidate).
Phase 3: Per item, immediately after GitHub succeeds, take a brief lock to
record the pointer_comment_id, versions, and timestamps. One commit per item
allows a pod eviction mid-pass to lose at most the item whose write just
returned; a rollback after twelve POSTs no longer re-posts twelve duplicates
on the next tick because each one's comment_id is recorded before the next
write begins.

A per-item failure (any exception from the GitHub write) records the failure
in its own locked transaction, then the loop continues to the next item.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os

import httpx
from sqlalchemy import func
from sqlmodel import Session, select

from factory.orchestration.factory_controls import _audit, _locked_session
from factory.orchestration.factory_models import FactoryAudit, WorkItem, WorkItemEvent

logger = logging.getLogger(__name__)

POINTER_ADVANCING_OPS = frozenset(("mint", "transition", "set_authority_local"))
POINTER_ERROR_THROTTLE = timedelta(hours=1)


def pointer_enabled() -> bool:
    return os.getenv("FACTORY_WORK_ITEM_POINTER_ENABLED", "false").lower() == "true"


def pointer_base_url() -> str:
    return os.getenv("FACTORY_WORK_ITEM_BASE_URL", "https://jomcgi.dev/slop")


def render_pointer(item: WorkItem) -> str:
    base_url = pointer_base_url().rstrip("/")
    return (
        f"<!-- work-item:{item.id} -->\n"
        f"Tracked as factory work item **{item.id}** ({item.state}) at "
        f"{base_url}/factory/work-items/{item.id}.\n\n"
        "This comment is a pointer, not a mirror: the work item is the record, "
        "and labels or comments here are not read back."
    )


def pointer_version(db: Session, item_id: int) -> int:
    version = db.exec(
        select(func.max(WorkItemEvent.version)).where(
            WorkItemEvent.work_item_id == item_id,
            WorkItemEvent.op.in_(POINTER_ADVANCING_OPS),
        )
    ).one()
    return int(version or 0)


def _github():
    """Return the lazy GitHub write seam used by pointer synchronization."""
    from factory.orchestration.factory_landing import github_write

    return github_write


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _comment_id(response: object) -> int:
    value = response.get("id") if isinstance(response, dict) else None
    if type(value) is not int or value <= 0:
        raise ValueError("GitHub comment response is missing a valid id")
    return value


def _audit_error(db: Session, actor: str, item: WorkItem, exc: Exception) -> None:
    last = db.exec(
        select(FactoryAudit)
        .where(FactoryAudit.action == "work_item_pointer_error")
        .order_by(FactoryAudit.id.desc())
    ).first()
    if last is not None and _aware(last.created_at) >= _now() - POINTER_ERROR_THROTTLE:
        return
    _audit(
        db,
        actor,
        "work_item_pointer_error",
        work_item_id=item.id,
        repo=item.github_repo,
        issue_number=item.github_issue_number,
        error=type(exc).__name__,
        status=getattr(getattr(exc, "response", None), "status_code", None),
    )


def sync_pointers(*, actor: str, limit: int = 20) -> dict:
    counts = {
        "created": 0,
        "updated": 0,
        "recreated": 0,
        "failed": 0,
        "skipped_disabled": 0,
    }
    if not pointer_enabled():
        counts["skipped_disabled"] = 1
        return counts

    candidates = []
    with _locked_session() as (db, _control):
        advancing_version = (
            select(func.max(WorkItemEvent.version))
            .where(
                WorkItemEvent.work_item_id == WorkItem.id,
                WorkItemEvent.op.in_(POINTER_ADVANCING_OPS),
            )
            .correlate(WorkItem)
            .scalar_subquery()
        )
        items = db.exec(
            select(WorkItem)
            .where(
                WorkItem.source_kind == "github",
                WorkItem.github_issue_number.is_not(None),
                WorkItem.state != "closed",
                WorkItem.pointer_synced_version < func.coalesce(advancing_version, 0),
                (
                    WorkItem.pointer_next_attempt_at.is_(None)
                    | (WorkItem.pointer_next_attempt_at <= func.now())
                ),
            )
            .order_by(WorkItem.updated_at, WorkItem.id)
            .limit(limit)
        ).all()
        if not items:
            return counts

        for item in items:
            version = pointer_version(db, item.id)
            payload = {"body": render_pointer(item)}
            candidates.append(
                (
                    item.id,
                    item.github_repo,
                    item.github_issue_number,
                    item.github_pointer_comment_id,
                    version,
                    payload,
                )
            )

    write = _github()
    for item_id, repo, issue_number, comment_id, version, payload in candidates:
        try:
            if comment_id is None:
                response = write(repo, f"issues/{issue_number}/comments", payload)
                new_comment_id = _comment_id(response)
                counts["created"] += 1
            else:
                try:
                    write(
                        repo, f"issues/comments/{comment_id}", payload, method="PATCH"
                    )
                    new_comment_id = comment_id
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
                    response = write(repo, f"issues/{issue_number}/comments", payload)
                    new_comment_id = _comment_id(response)
                    counts["recreated"] += 1
                else:
                    counts["updated"] += 1

            with _locked_session() as (db, _control):
                item = db.get(WorkItem, item_id)
                if item is not None:
                    item.github_pointer_comment_id = new_comment_id
                    item.pointer_synced_version = version
                    item.pointer_synced_at = _now()
                    item.pointer_failures = 0
                    item.pointer_next_attempt_at = None
                    db.add(item)
                    db.commit()

        except Exception as exc:  # noqa: BLE001 - isolate each GitHub write
            counts["failed"] += 1
            logger.exception("work item pointer sync failed for item %s", item_id)

            with _locked_session() as (db, _control):
                item = db.get(WorkItem, item_id)
                if item is not None:
                    item.pointer_failures += 1
                    backoff_minutes = min(2**item.pointer_failures, 1440)
                    item.pointer_next_attempt_at = _now() + timedelta(
                        minutes=backoff_minutes
                    )
                    db.add(item)
                    db.commit()
                    _audit_error(db, actor, item, exc)

    return counts
