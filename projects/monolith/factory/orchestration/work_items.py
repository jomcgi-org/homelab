"""Durable work item semantics and GitHub-authority synchronization."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core.db import get_engine  # noqa: F401 - for test monkeypatching
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from factory.orchestration.factory_controls import (
    _locked_session,
    normalize_repo,
    receipt_task_class,
)
from factory.orchestration.factory_intake_loop import derive_task_class
from factory.orchestration.factory_models import (
    FactoryGithubIssueState,
    FactoryReceipt,
    WorkItem,
    WorkItemEdge,
    WorkItemEvent,
)

_STATES = frozenset(
    ("open", "ready", "deferred", "needs_human", "active", "done", "closed")
)
_CLOSE_REASONS = frozenset(
    ("completed", "not_planned", "superseded", "stale", "github_closed")
)
_EDGE_KINDS = frozenset(("blocks", "parent", "supersedes"))
_TRANSITIONS = {
    "open": frozenset(("ready", "deferred", "needs_human", "closed")),
    "ready": frozenset(("open", "deferred", "needs_human", "active", "closed")),
    "deferred": frozenset(("open", "ready", "needs_human", "closed")),
    "needs_human": frozenset(("open", "ready", "deferred", "closed")),
    "active": frozenset(("done", "ready", "needs_human", "closed")),
    "done": frozenset(("closed", "open")),
    "closed": frozenset(("open",)),
}


class WorkItemError(ValueError):
    """A requested work item mutation violates semantic invariants."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def trust_for_github_author(
    author: dict | None, *, trusted_authors: frozenset[str] | None = None
) -> str:
    """Map a GitHub user object to the work item's ingestion trust."""
    if not isinstance(author, dict):
        return "untrusted"
    login = author.get("login")
    configured = trusted_authors or frozenset(("jomcgi",))
    if isinstance(login, str) and login.lower() in configured:
        return "trusted"
    if author.get("type") == "Bot" or (
        isinstance(login, str)
        and (login.lower() == "dependabot" or login.lower().endswith("[bot]"))
    ):
        return "semi_trusted"
    return "untrusted"


def state_from_github_labels(labels: set[str]) -> str:
    """Translate the label-owned GitHub state using explicit precedence."""
    normalized = {label.lower() for label in labels}
    if "needs-human" in normalized:
        return "needs_human"
    if "needs-thought" in normalized:
        return "deferred"
    if "agent-ready" in normalized:
        return "ready"
    return "open"


def _issue_number(issue: dict) -> int:
    number = issue.get("number")
    if type(number) is not int or number <= 0:
        raise WorkItemError("GitHub issue number must be a positive integer")
    return number


def _issue_labels(issue: dict) -> set[str]:
    labels: set[str] = set()
    raw = issue.get("labels") or []
    if not isinstance(raw, list):
        raise WorkItemError("GitHub issue labels must be a list")
    for label in raw:
        name = label.get("name") if isinstance(label, dict) else label
        if not isinstance(name, str):
            raise WorkItemError("GitHub issue label names must be strings")
        labels.add(name.lower())
    return labels


def _github_created_at(issue: dict) -> datetime | None:
    raw = issue.get("created_at")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise WorkItemError("GitHub issue created_at must be an ISO timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkItemError("GitHub issue created_at must be an ISO timestamp") from exc
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _same_value(left: Any, right: Any) -> bool:
    """Compare persisted values without treating SQLite timezone loss as a change."""
    if isinstance(left, datetime) and isinstance(right, datetime):
        if left.tzinfo is None:
            left = left.replace(tzinfo=timezone.utc)
        if right.tzinfo is None:
            right = right.replace(tzinfo=timezone.utc)
        return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)
    return left == right


def github_issue_updated_at(issue: dict) -> datetime | None:
    """Return GitHub's source-order timestamp without inventing a fallback."""
    raw = issue.get("updated_at")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise WorkItemError("GitHub issue updated_at must be an ISO timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkItemError("GitHub issue updated_at must be an ISO timestamp") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def order_github_issue_snapshot(
    db: Session,
    repo: str,
    issue: dict,
    *,
    source_ref: str,
) -> tuple[FactoryGithubIssueState | None, str | None]:
    """Lock and advance one issue watermark, or reject a non-new snapshot.

    Missing source timestamps never mutate repository state. Equal timestamps
    are conservatively treated as stale because GitHub supplies no ordering
    within a tie. A legitimate reopen therefore needs a strictly newer
    issue.updated_at than the recorded close.
    """
    number = _issue_number(issue)
    source_updated_at = github_issue_updated_at(issue)
    if source_updated_at is None:
        return None, "missing_timestamp_ignored"
    source_state = issue.get("state")
    if source_state not in ("open", "closed"):
        raise WorkItemError("GitHub issue state must be open or closed")

    row = db.exec(
        select(FactoryGithubIssueState)
        .where(
            FactoryGithubIssueState.repo == repo,
            FactoryGithubIssueState.issue_number == number,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    created = False
    if row is None:
        candidate = FactoryGithubIssueState(
            repo=repo,
            issue_number=number,
            source_updated_at=source_updated_at,
            source_state=source_state,
            source_ref=source_ref,
            updated_at=_now(),
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            row = candidate
            created = True
        except IntegrityError:
            # A distinct first delivery for the same issue won the insert.
            # The savepoint preserves this transaction's delivery claim.
            row = db.exec(
                select(FactoryGithubIssueState)
                .where(
                    FactoryGithubIssueState.repo == repo,
                    FactoryGithubIssueState.issue_number == number,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()

    recorded_updated_at = row.source_updated_at
    if recorded_updated_at.tzinfo is None:
        recorded_updated_at = recorded_updated_at.replace(tzinfo=timezone.utc)
    else:
        recorded_updated_at = recorded_updated_at.astimezone(timezone.utc)
    if not created and source_updated_at <= recorded_updated_at:
        return row, "stale_ignored"
    if not created:
        row.source_updated_at = source_updated_at
        row.source_state = source_state
        row.source_ref = source_ref
        row.updated_at = _now()
        db.add(row)
    return row, None


def _github_values(
    repo: str,
    issue: dict,
    *,
    trusted_authors: frozenset[str] | None = None,
) -> dict[str, Any]:
    number = _issue_number(issue)
    title = issue.get("title")
    body = issue.get("body") or ""
    if not isinstance(title, str) or not title:
        raise WorkItemError("GitHub issue title must be non-empty")
    if not isinstance(body, str):
        raise WorkItemError("GitHub issue body must be text")
    labels = _issue_labels(issue)
    source_ref = issue.get("html_url") or f"https://github.com/{repo}/issues/{number}"
    if not isinstance(source_ref, str):
        raise WorkItemError("GitHub issue URL must be text")
    task_class, _reason = derive_task_class(labels, refine=False)
    return {
        "title": title,
        "body": body,
        "state": state_from_github_labels(labels),
        "task_class": task_class,
        "labels": sorted(labels),
        "source_ref": source_ref,
        "trust": trust_for_github_author(
            issue.get("user") or issue.get("author"),
            trusted_authors=trusted_authors,
        ),
        "github_created_at": _github_created_at(issue),
    }


def _lock_item(db: Session, item_id: int) -> WorkItem:
    item = db.exec(
        select(WorkItem).where(WorkItem.id == item_id).with_for_update()
    ).one_or_none()
    if item is None:
        raise WorkItemError(f"work item {item_id} not found")
    return item


def _lock_items(db: Session, *item_ids: int) -> dict[int, WorkItem]:
    wanted = sorted(set(item_ids))
    rows = db.exec(
        select(WorkItem)
        .where(WorkItem.id.in_(wanted))
        .order_by(WorkItem.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    found = {item.id: item for item in rows if item.id is not None}
    missing = [item_id for item_id in wanted if item_id not in found]
    if missing:
        raise WorkItemError(f"work item {missing[0]} not found")
    return found


def _lock_github_repo_items(db: Session, repo: str) -> dict[int, WorkItem]:
    """Lock GitHub work items after source fences, in stable ID order."""
    rows = db.exec(
        select(WorkItem)
        .where(
            WorkItem.github_repo == repo,
            WorkItem.github_issue_number.is_not(None),
        )
        .order_by(WorkItem.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {
        item.github_issue_number: item
        for item in rows
        if item.github_issue_number is not None
    }


def _event(
    db: Session,
    item: WorkItem,
    op: str,
    change: dict[str, Any],
    *,
    actor: str,
    author_kind: str,
    cause_kind: str,
    cause_ref: str | None,
    stated_reason: str | None,
) -> None:
    if item.id is None:
        raise WorkItemError("work item must be flushed before recording an event")
    current = db.exec(
        select(func.max(WorkItemEvent.version)).where(
            WorkItemEvent.work_item_id == item.id
        )
    ).one()
    db.add(
        WorkItemEvent(
            work_item_id=item.id,
            version=int(current or 0) + 1,
            op=op,
            author_kind=author_kind,
            author=actor,
            change_json=json.dumps(change, sort_keys=True, separators=(",", ":")),
            cause_kind=cause_kind,
            cause_ref=cause_ref,
            stated_reason=stated_reason,
        )
    )


def _mint_or_sync_from_github_locked(
    db: Session,
    repo: str,
    issue: dict,
    item: WorkItem | None,
    *,
    actor: str,
    trusted_authors: frozenset[str] | None = None,
) -> tuple[WorkItem | None, str]:
    """Apply one snapshot after its source fence and item lock are held."""
    number = _issue_number(issue)
    if item is not None and item.authority == "local":
        return item, "local_untouched"

    values = _github_values(repo, issue, trusted_authors=trusted_authors)
    now = _now()
    if item is None:
        item = WorkItem(
            **values,
            source_kind="github",
            authority="github",
            github_repo=repo,
            github_issue_number=number,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        db.flush()
        _event(
            db,
            item,
            "mint",
            {
                "github_issue_number": number,
                "github_repo": repo,
                "state": item.state,
            },
            actor=actor,
            author_kind="github",
            cause_kind="github_sync",
            cause_ref=item.source_ref,
            stated_reason="minted from GitHub issue",
        )
        db.exec(
            update(FactoryReceipt)
            .where(
                FactoryReceipt.repo == repo,
                FactoryReceipt.issue_number == number,
                FactoryReceipt.work_item_id.is_(None),
            )
            .values(work_item_id=item.id)
        )
        return item, "minted"

    changes: dict[str, Any] = {}
    transitioned = False
    labels_changed = item.labels != values["labels"]
    for field in (
        "title",
        "body",
        "task_class",
        "labels",
        "source_ref",
        "trust",
        "github_created_at",
    ):
        value = values[field]
        if not _same_value(getattr(item, field), value):
            setattr(item, field, value)
            changes[field] = value.isoformat() if isinstance(value, datetime) else value
    label_state = values["state"]
    if item.state in ("active", "done"):
        if labels_changed and label_state != item.state:
            changes["label_state"] = label_state
    elif item.state != label_state:
        # Apply the shortest legal path to reach label_state, at most 2 steps
        current = item.state
        target = label_state
        path = None

        # Direct transition possible?
        if target in _TRANSITIONS[current]:
            path = [target]
        # Two-step path possible? (only closed -> open -> X)
        elif current == "closed" and "open" in _TRANSITIONS[current]:
            if target in _TRANSITIONS["open"]:
                path = ["open", target]

        if path:
            # Apply transitions
            for next_state in path:
                transition(
                    db,
                    item.id,
                    next_state,
                    actor="system",
                    author_kind="system",
                    cause_kind="github_sync",
                    cause_ref=item.source_ref,
                    stated_reason=f"GitHub labels: {', '.join(sorted(values['labels']))}",
                )
            transitioned = True
            # Reload the item after transitions
            item = _lock_item(db, item.id)
        else:
            # Label state is unreachable; record it in sync event without changing state
            changes["label_state"] = label_state
    if not changes:
        return item, "synced" if transitioned else "unchanged"
    item.updated_at = now
    db.add(item)
    _event(
        db,
        item,
        "sync",
        changes,
        actor=actor,
        author_kind="github",
        cause_kind="github_sync",
        cause_ref=item.source_ref,
        stated_reason="synced from GitHub issue",
    )
    return item, "synced"


def mint_or_sync_from_github(
    db: Session,
    repo: str,
    issue: dict,
    *,
    actor: str,
    trusted_authors: frozenset[str] | None = None,
    source_ordered: bool = False,
    source_ref: str | None = None,
) -> tuple[WorkItem | None, str]:
    """Mint or refresh one issue while respecting a local authority handoff."""
    if issue.get("pull_request") is not None:
        return None, "skipped"
    try:
        repo = normalize_repo(repo)
    except ValueError as exc:
        raise WorkItemError("invalid GitHub repository") from exc
    number = _issue_number(issue)
    ordering_outcome = None
    if source_ordered:
        _source_state, ordering_outcome = order_github_issue_snapshot(
            db,
            repo,
            issue,
            source_ref=source_ref or actor,
        )
        # Source fences always precede work-item locks. Locking the repository
        # set also gives dependency reconciliation the same stable ID order.
        item = _lock_github_repo_items(db, repo).get(number)
    else:
        item = db.exec(
            select(WorkItem)
            .where(
                WorkItem.github_repo == repo,
                WorkItem.github_issue_number == number,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
    if item is not None and item.authority == "local":
        return item, "local_untouched"
    if ordering_outcome is not None:
        return item, ordering_outcome
    return _mint_or_sync_from_github_locked(
        db,
        repo,
        issue,
        item,
        actor=actor,
        trusted_authors=trusted_authors,
    )


def transition(
    db: Session,
    item_id: int,
    to_state: str,
    *,
    actor: str,
    author_kind: str,
    cause_kind: str,
    cause_ref: str | None = None,
    stated_reason: str | None = None,
    close_reason: str | None = None,
) -> WorkItem:
    """Apply one legal state-machine edge and append its version event."""
    if to_state not in _STATES:
        raise WorkItemError(f"unknown work item state {to_state}")
    item = _lock_item(db, item_id)
    if to_state not in _TRANSITIONS[item.state]:
        raise WorkItemError(f"illegal work item transition {item.state} -> {to_state}")
    if to_state == "closed" and close_reason not in _CLOSE_REASONS:
        raise WorkItemError("closing a work item requires a valid close reason")
    if to_state != "closed" and close_reason is not None:
        raise WorkItemError("close reason is only valid when closing a work item")
    now = _now()
    old_state = item.state
    item.state = to_state
    item.close_reason = close_reason if to_state == "closed" else None
    item.closed_at = now if to_state == "closed" else None
    item.updated_at = now
    db.add(item)
    change: dict[str, Any] = {"state": to_state}
    if to_state == "closed":
        change["close_reason"] = close_reason
    elif old_state == "closed":
        change["close_reason"] = None
    _event(
        db,
        item,
        "transition",
        change,
        actor=actor,
        author_kind=author_kind,
        cause_kind=cause_kind,
        cause_ref=cause_ref,
        stated_reason=stated_reason,
    )
    return item


def _would_cycle(db: Session, from_id: int, to_id: int, kind: str) -> bool:
    pending = [to_id]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current == from_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(
            db.exec(
                select(WorkItemEdge.to_id).where(
                    WorkItemEdge.from_id == current,
                    WorkItemEdge.kind == kind,
                )
            ).all()
        )
    return False


def add_edge(
    db: Session,
    from_id: int,
    to_id: int,
    kind: str,
    *,
    actor: str,
    author_kind: str,
    cause_kind: str,
    cause_ref: str | None = None,
    stated_reason: str | None = None,
    source: str = "manual",
) -> None:
    """Add one relationship, idempotently, after checking relevant cycles."""
    if kind not in _EDGE_KINDS:
        raise WorkItemError(f"unknown work item edge kind {kind}")
    if from_id == to_id:
        raise WorkItemError("work item edge cannot reference itself")
    items = _lock_items(db, from_id, to_id)
    existing = db.exec(
        select(WorkItemEdge).where(
            WorkItemEdge.from_id == from_id,
            WorkItemEdge.to_id == to_id,
            WorkItemEdge.kind == kind,
        )
    ).one_or_none()
    if existing is not None:
        return
    if kind != "supersedes" and _would_cycle(db, from_id, to_id, kind):
        raise WorkItemError(f"would create {kind} cycle")
    db.add(WorkItemEdge(from_id=from_id, to_id=to_id, kind=kind, source=source))
    _event(
        db,
        items[from_id],
        "add_edge",
        {"edge": {"from_id": from_id, "kind": kind, "to_id": to_id}},
        actor=actor,
        author_kind=author_kind,
        cause_kind=cause_kind,
        cause_ref=cause_ref,
        stated_reason=stated_reason,
    )


def remove_edge(
    db: Session,
    from_id: int,
    to_id: int,
    kind: str,
    *,
    actor: str,
    author_kind: str,
    cause_kind: str,
    cause_ref: str | None = None,
    stated_reason: str | None = None,
) -> None:
    """Remove one relationship idempotently and record an applied removal."""
    if kind not in _EDGE_KINDS:
        raise WorkItemError(f"unknown work item edge kind {kind}")
    items = _lock_items(db, from_id, to_id)
    edge = db.exec(
        select(WorkItemEdge).where(
            WorkItemEdge.from_id == from_id,
            WorkItemEdge.to_id == to_id,
            WorkItemEdge.kind == kind,
        )
    ).one_or_none()
    if edge is None:
        return
    db.delete(edge)
    _event(
        db,
        items[from_id],
        "remove_edge",
        {"edge": {"from_id": from_id, "kind": kind, "to_id": to_id}},
        actor=actor,
        author_kind=author_kind,
        cause_kind=cause_kind,
        cause_ref=cause_ref,
        stated_reason=stated_reason,
    )


def set_authority_local(
    db: Session,
    item_id: int,
    *,
    actor: str,
    author_kind: str,
    cause_kind: str,
    cause_ref: str | None = None,
    stated_reason: str | None = None,
) -> WorkItem:
    """Permanently hand an imported work item to local authority."""
    item = _lock_item(db, item_id)
    if item.authority == "local":
        return item
    item.authority = "local"
    item.updated_at = _now()
    db.add(item)
    _event(
        db,
        item,
        "set_authority_local",
        {"authority": "local"},
        actor=actor,
        author_kind=author_kind,
        cause_kind=cause_kind,
        cause_ref=cause_ref,
        stated_reason=stated_reason,
    )
    return item


def open_blockers(db: Session, item_id: int) -> list[WorkItem]:
    """Return non-closed work items with a blocks edge into this item."""
    return list(
        db.exec(
            select(WorkItem)
            .join(WorkItemEdge, WorkItemEdge.from_id == WorkItem.id)
            .where(
                WorkItemEdge.to_id == item_id,
                WorkItemEdge.kind == "blocks",
                WorkItem.state != "closed",
            )
            .order_by(WorkItem.id)
        ).all()
    )


def is_admissible(db: Session, item_id: int) -> bool:
    """An item is admissible exactly when ready and free of open blockers."""
    item = db.exec(select(WorkItem).where(WorkItem.id == item_id)).one_or_none()
    if item is None:
        return False
    return item.state == "ready" and not open_blockers(db, item_id)


def _work_item_document(db: Session, item: WorkItem) -> dict:
    """Shape one work item with its relationships and bounded event history."""
    edges_out = db.exec(
        select(WorkItemEdge)
        .where(WorkItemEdge.from_id == item.id)
        .order_by(WorkItemEdge.id)
    ).all()
    edges_in = db.exec(
        select(WorkItemEdge)
        .where(WorkItemEdge.to_id == item.id)
        .order_by(WorkItemEdge.id)
    ).all()
    events = db.exec(
        select(WorkItemEvent)
        .where(WorkItemEvent.work_item_id == item.id)
        .order_by(WorkItemEvent.created_at.desc(), WorkItemEvent.id.desc())
        .limit(20)
    ).all()
    receipts = db.exec(
        select(FactoryReceipt)
        .where(FactoryReceipt.work_item_id == item.id)
        .order_by(FactoryReceipt.created_at.desc(), FactoryReceipt.id.desc())
    ).all()
    return {
        "item": item.model_dump(),
        "edges_out": [
            {
                "id": edge.id,
                "to_id": edge.to_id,
                "kind": edge.kind,
                "source": edge.source,
                "created_at": edge.created_at.isoformat(),
            }
            for edge in edges_out
        ],
        "edges_in": [
            {
                "id": edge.id,
                "from_id": edge.from_id,
                "kind": edge.kind,
                "source": edge.source,
                "created_at": edge.created_at.isoformat(),
            }
            for edge in edges_in
        ],
        "events": [
            {
                "id": event.id,
                "version": event.version,
                "op": event.op,
                "author_kind": event.author_kind,
                "author": event.author,
                "change_json": event.change_json,
                "cause_kind": event.cause_kind,
                "cause_ref": event.cause_ref,
                "stated_reason": event.stated_reason,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ],
        "receipts": [
            {
                "id": receipt.id,
                "generation": receipt.generation,
                "task_class": receipt_task_class(receipt),
                "state": receipt.state,
                "created_at": receipt.created_at.isoformat(),
                "task_id": receipt.task_id,
            }
            for receipt in receipts
        ],
    }


def work_item_document(db: Session, item_id: int) -> dict | None:
    """Return one work item read model, or None when the item is unknown."""
    item = db.exec(select(WorkItem).where(WorkItem.id == item_id)).one_or_none()
    return None if item is None else _work_item_document(db, item)


def list_work_items(
    db: Session,
    *,
    state: str | None,
    authority: str | None,
    limit: int,
) -> list[dict]:
    """Return bounded work item rows in newest-first order."""
    query = select(WorkItem)
    if state is not None:
        query = query.where(WorkItem.state == state)
    if authority is not None:
        query = query.where(WorkItem.authority == authority)
    rows = db.exec(
        query.order_by(WorkItem.created_at.desc(), WorkItem.id.desc()).limit(
            max(1, min(int(limit), 200))
        )
    ).all()
    return [item.model_dump() for item in rows]


def close_missing_from_github(
    db: Session, repo: str, open_numbers: set[int], *, actor: str
) -> int:
    """Close GitHub-authority items absent from a complete open-issue listing."""
    # An empty listing is never trusted as "everything closed": a repository
    # with no open issues would close every row, and there is no floor under
    # that. Callers see it as close_skipped=empty_listing.
    if not open_numbers:
        return 0

    try:
        repo = normalize_repo(repo)
    except ValueError as exc:
        raise WorkItemError("invalid GitHub repository") from exc
    rows = db.exec(
        select(WorkItem)
        .where(
            WorkItem.github_repo == repo,
            WorkItem.authority == "github",
            WorkItem.state != "closed",
            WorkItem.github_issue_number.not_in(open_numbers),
        )
        .order_by(WorkItem.id)
    ).all()
    for row in rows:
        transition(
            db,
            row.id,
            "closed",
            actor=actor,
            author_kind="factory",
            cause_kind="github_sync",
            cause_ref=f"github:{repo}",
            stated_reason="issue absent from complete GitHub open listing",
            close_reason="github_closed",
        )
    return len(rows)


def sync_github_work_items(
    repo: str,
    issues: list,
    *,
    truncated: bool,
    actor: str,
    source_ordered: bool = False,
) -> dict[str, int | str | None]:
    """Synchronize one bounded GitHub listing in a single transaction."""
    try:
        repo = normalize_repo(repo)
    except ValueError as exc:
        raise WorkItemError("invalid GitHub repository") from exc
    counts = {
        "minted": 0,
        "synced": 0,
        "unchanged": 0,
        "local_untouched": 0,
        "stale_ignored": 0,
        "missing_timestamp_ignored": 0,
        "skipped": 0,
        "closed": 0,
        "close_skipped": None,
    }
    open_numbers: set[int] = set()
    with _locked_session() as (db, _control):
        indexed_issues: list[tuple[int, dict]] = []
        for index, issue in enumerate(issues):
            if not isinstance(issue, dict):
                raise WorkItemError("GitHub issue entries must be objects")
            if issue.get("pull_request") is not None:
                counts["skipped"] += 1
                continue
            _issue_number(issue)
            indexed_issues.append((index, issue))

        ordering_outcomes: dict[int, str | None] = {}
        if source_ordered:
            # Acquire every issue fence in source identity order before taking
            # any work-item lock. This matches the webhook lock order and
            # prevents a multi-issue sweep from deadlocking with delivery
            # dependency reconciliation.
            for index, issue in sorted(
                indexed_issues, key=lambda indexed: _issue_number(indexed[1])
            ):
                _source_state, ordering_outcomes[index] = order_github_issue_snapshot(
                    db,
                    repo,
                    issue,
                    source_ref="github:sweep",
                )

        locked_items = _lock_github_repo_items(db, repo)
        for index, issue in indexed_issues:
            number = _issue_number(issue)
            item = locked_items.get(number)
            if item is not None and item.authority == "local":
                outcome = "local_untouched"
            elif ordering_outcomes.get(index) is not None:
                outcome = ordering_outcomes[index]
            else:
                item, outcome = _mint_or_sync_from_github_locked(
                    db,
                    repo,
                    issue,
                    item,
                    actor=actor,
                )
                if item is not None:
                    locked_items[number] = item
            counts[outcome] += 1
            if item is not None:
                open_numbers.add(number)
        if not truncated and not source_ordered:
            closed_count = close_missing_from_github(
                db, repo, open_numbers, actor=actor
            )
            if not open_numbers:
                counts["close_skipped"] = "empty_listing"
            counts["closed"] = closed_count
        elif source_ordered:
            # Absence from an open-issue listing carries no issue.updated_at,
            # so it cannot safely advance the same source-order watermark.
            # Webhook close events remain authoritative while ingress is on.
            counts["close_skipped"] = "source_ordered"
        db.commit()
    return counts
