"""Staged GitHub issue webhook intake for durable factory work items.

The route is inert unless explicitly enabled. GitHub authenticates each exact
request body with HMAC-SHA256. A delivery claim and its work-item mutation share
one transaction: a processing failure rolls both back, so a legitimate retry is
never suppressed by a prematurely committed deduplication row.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime

from core.db import get_session
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from factory.orchestration.factory_models import FactoryWebhookDelivery, WorkItem
from factory.orchestration.work_item_links import reconcile_body_edges
from factory.orchestration.work_items import (
    WorkItemError,
    _lock_github_repo_items,
    _mint_or_sync_from_github_locked,
    github_issue_updated_at,
    order_github_issue_snapshot,
    transition,
    trust_for_github_author,
)

router = APIRouter(prefix="/webhooks/github", tags=["factory-webhook"])

MAX_PAYLOAD_BYTES = 1_048_576
_DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_SUPPORTED_ACTIONS = frozenset(
    ("opened", "edited", "labeled", "unlabeled", "reopened", "closed")
)


def webhook_enabled() -> bool:
    return os.getenv("FACTORY_GITHUB_WEBHOOK_ENABLED", "false").lower() == "true"


def webhook_repository() -> str:
    return os.getenv("FACTORY_GITHUB_WEBHOOK_REPOSITORY", "").strip().lower()


def trusted_authors() -> frozenset[str]:
    return frozenset(
        login.strip().lower()
        for login in os.getenv("FACTORY_GITHUB_WEBHOOK_TRUSTED_AUTHORS", "").split(",")
        if login.strip()
    )


def _verify_signature(body: bytes, signature: str | None) -> None:
    secret = os.getenv("FACTORY_GITHUB_WEBHOOK_SECRET", "")
    if not secret or not signature or not signature.startswith("sha256="):
        raise HTTPException(401, "invalid webhook signature")
    supplied = signature.removeprefix("sha256=")
    if len(supplied) != 64 or any(c not in "0123456789abcdefABCDEF" for c in supplied):
        raise HTTPException(401, "invalid webhook signature")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied.lower()):
        raise HTTPException(401, "invalid webhook signature")


async def _bounded_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_PAYLOAD_BYTES:
                raise HTTPException(413, "webhook payload too large")
        except ValueError as exc:
            raise HTTPException(400, "invalid content length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_PAYLOAD_BYTES:
            raise HTTPException(413, "webhook payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _payload(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "invalid JSON payload") from exc
    if not isinstance(value, dict):
        raise HTTPException(400, "webhook payload must be an object")
    return value


def _repo(payload: dict) -> str:
    repository = payload.get("repository")
    value = repository.get("full_name") if isinstance(repository, dict) else None
    if not isinstance(value, str) or value.lower() != webhook_repository():
        raise HTTPException(403, "unexpected webhook repository")
    return value.lower()


def _issue(payload: dict) -> dict:
    issue = payload.get("issue")
    if not isinstance(issue, dict) or issue.get("pull_request") is not None:
        raise HTTPException(422, "event does not contain a GitHub issue")
    number = issue.get("number")
    if type(number) is not int or number <= 0:
        raise HTTPException(422, "invalid GitHub issue identity")
    return issue


def _reconcile_stored_body_edges(session: Session, repo: str) -> dict:
    """Reconcile dependencies from the complete stored repository view."""
    items = session.exec(
        select(WorkItem).where(
            WorkItem.github_repo == repo,
            WorkItem.github_issue_number.is_not(None),
        )
    ).all()
    return reconcile_body_edges(
        session,
        repo,
        [(item, item.body) for item in items],
        actor="github:webhook",
    )


def _claim_delivery(
    session: Session,
    *,
    delivery_id: str,
    event: str,
    action: str | None,
    repo: str,
    issue_number: int | None,
    source_updated_at: datetime | None = None,
) -> FactoryWebhookDelivery | None:
    row = FactoryWebhookDelivery(
        delivery_id=delivery_id,
        event=event,
        action=action,
        repo=repo,
        issue_number=issue_number,
        source_updated_at=source_updated_at,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return row


def process_delivery(
    session: Session,
    *,
    delivery_id: str,
    event: str,
    payload: dict,
) -> dict:
    """Apply one validated delivery and commit its deduplication fence."""
    repo = _repo(payload)
    raw_action = payload.get("action")
    action = raw_action if isinstance(raw_action, str) else None

    if event != "issues":
        claim = _claim_delivery(
            session,
            delivery_id=delivery_id,
            event=event,
            action=action,
            repo=repo,
            issue_number=None,
        )
        if claim is None:
            return {"status": "duplicate"}
        claim.outcome = "ignored_event"
        session.add(claim)
        session.commit()
        return {"status": "ignored", "reason": "unsupported_event"}

    issue = _issue(payload)
    number = issue["number"]
    try:
        source_updated_at = github_issue_updated_at(issue)
    except WorkItemError as exc:
        raise HTTPException(422, str(exc)) from exc
    claim = _claim_delivery(
        session,
        delivery_id=delivery_id,
        event=event,
        action=action,
        repo=repo,
        issue_number=number,
        source_updated_at=source_updated_at,
    )
    if claim is None:
        return {"status": "duplicate"}
    if action not in _SUPPORTED_ACTIONS:
        claim.outcome = "ignored_action"
        session.add(claim)
        session.commit()
        return {"status": "ignored", "reason": "unsupported_action"}

    state = issue.get("state")
    if state not in ("open", "closed") or action == "closed" and state != "closed":
        raise HTTPException(422, "issue lifecycle state is inconsistent")

    trust = trust_for_github_author(
        issue.get("user"), trusted_authors=trusted_authors()
    )
    if trust == "untrusted":
        claim.outcome = "untrusted_ignored"
        session.add(claim)
        session.commit()
        return {"status": "accepted", "outcome": claim.outcome}
    if trust == "semi_trusted":
        claim.outcome = "semi_trusted_held"
        session.add(claim)
        session.commit()
        return {"status": "accepted", "outcome": claim.outcome}

    try:
        _source_state, ordering_outcome = order_github_issue_snapshot(
            session,
            repo,
            issue,
            source_ref=f"delivery:{delivery_id}",
        )
    except WorkItemError as exc:
        raise HTTPException(422, str(exc)) from exc
    # The per-issue source fence is always acquired before any work-item lock.
    # Fetch existence and authority only after a possible wait on that fence.
    existing = _lock_github_repo_items(session, repo).get(number)
    if existing is not None and existing.authority == "local":
        claim.outcome = "local_untouched"
        claim.work_item_id = existing.id
        session.add(claim)
        session.commit()
        return {
            "status": "accepted",
            "outcome": claim.outcome,
            "work_item_id": existing.id,
        }
    if ordering_outcome is not None:
        claim.outcome = f"trusted_{ordering_outcome}"
        if existing is not None:
            claim.work_item_id = existing.id
        session.add(claim)
        session.commit()
        return {
            "status": "accepted",
            "outcome": claim.outcome,
            **({"work_item_id": claim.work_item_id} if claim.work_item_id else {}),
        }

    if state == "closed":
        if existing is None:
            claim.outcome = "trusted_closed"
        elif existing.state == "closed":
            claim.outcome = "trusted_unchanged"
            claim.work_item_id = existing.id
        else:
            item = transition(
                session,
                existing.id,
                "closed",
                actor="github:webhook",
                author_kind="github",
                cause_kind="github_webhook",
                cause_ref=delivery_id,
                stated_reason="GitHub issue closed",
                close_reason="github_closed",
            )
            claim.outcome = "trusted_closed"
            claim.work_item_id = item.id
    else:
        try:
            item, outcome = _mint_or_sync_from_github_locked(
                session,
                repo,
                issue,
                existing,
                actor="github:webhook",
                trusted_authors=trusted_authors(),
            )
        except WorkItemError as exc:
            raise HTTPException(422, str(exc)) from exc
        if item is None:
            raise HTTPException(422, "event does not contain a supported issue")
        claim.outcome = (
            "local_untouched" if outcome == "local_untouched" else f"trusted_{outcome}"
        )
        claim.work_item_id = item.id
        if item.authority == "github":
            _reconcile_stored_body_edges(session, repo)

    session.add(claim)
    session.commit()
    return {
        "status": "accepted",
        "outcome": claim.outcome,
        **({"work_item_id": claim.work_item_id} if claim.work_item_id else {}),
    }


@router.post("/factory")
async def github_factory_webhook(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008 - FastAPI dependency
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict:
    """Authenticate and ingest one bounded GitHub issue lifecycle delivery."""
    if not webhook_enabled():
        raise HTTPException(404, "factory webhook disabled")
    if not webhook_repository() or not trusted_authors():
        raise HTTPException(503, "factory webhook is not configured")
    if not isinstance(x_github_event, str) or not x_github_event:
        raise HTTPException(400, "missing GitHub event")
    if not isinstance(x_github_delivery, str) or not _DELIVERY_ID.fullmatch(
        x_github_delivery
    ):
        raise HTTPException(400, "invalid GitHub delivery id")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        raise HTTPException(415, "webhook content type must be application/json")
    body = await _bounded_body(request)
    _verify_signature(body, x_hub_signature_256)
    payload = _payload(body)
    try:
        return process_delivery(
            session,
            delivery_id=x_github_delivery,
            event=x_github_event,
            payload=payload,
        )
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
