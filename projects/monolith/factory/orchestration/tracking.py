"""Private information recording. No network, receipt, executor or scheduler calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from core.clock import aware_iso
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from factory.orchestration.factory_models import (
    TrackingRequest,
    WorkItem,
    WorkItemEvent,
)

Text = Annotated[str, Field(strict=True, max_length=2000)]
Key = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("an integer is required without coercion")
    return value


Version = Annotated[
    int, BeforeValidator(_integer), Field(strict=True, ge=1, le=2**31 - 1)
]
State = Literal["open", "ready", "deferred", "needs_human", "active", "done", "closed"]


class TrackingError(ValueError):
    """A bounded recording request was refused without effects."""


def source_url(value: str) -> str:
    """Canonical identity: HTTPS host case/default port/fragment only."""
    if (
        not isinstance(value, str)
        or len(value) > 1000
        or "\\" in value
        or any(ord(c) < 33 for c in value)
    ):
        raise ValueError("source_ref must be a bounded HTTPS URL")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("source_ref must be an HTTPS URL without credentials")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    canonical = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    if len(canonical) > 1000:
        raise ValueError("canonical source_ref must be at most 1000 characters")
    return canonical


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: Key
    title: Annotated[str, Field(min_length=1, max_length=512)]
    source_ref: Annotated[str, Field(min_length=1, max_length=1000)]
    tracking_kind: Literal["manual", "external"]
    owner: Annotated[str, Field(min_length=1, max_length=256)]
    body: Text = ""
    blocker: Text = ""
    next_action: Text = ""

    _source = field_validator("source_ref")(source_url)

    @field_validator("title", "owner")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: Key
    expected_version: Version
    state: State
    title: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    blocker: Text | None = None
    next_action: Text | None = None
    completion_evidence: Annotated[str, Field(min_length=1, max_length=2000)] | None = (
        None
    )

    @field_validator("title", "completion_evidence")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value


_OPEN = frozenset(("open", "ready", "deferred", "needs_human", "active"))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def document(item: WorkItem) -> dict:
    fields = (
        "id",
        "title",
        "body",
        "source_kind",
        "source_ref",
        "execution_mode",
        "tracking_kind",
        "owner",
        "state",
        "blocker",
        "next_action",
        "completion_evidence",
        "tracking_version",
    )
    result = {key: getattr(item, key) for key in fields}
    for key in ("created_at", "updated_at", "completed_at", "closed_at"):
        value = getattr(item, key)
        result[key] = aware_iso(value) if value is not None else None
    return result


def record(
    engine, actor: str, body: Create | Change, *, item_id: int | None = None
) -> dict:
    """Unique request insert serializes retries; CAS serializes different writers.

    The reservation, result and event commit together. No pending reservation
    survives an exception. Replays return the original snapshot, not today's row.
    """
    payload = _json(
        {"item_id": item_id, "operation": type(body).__name__, **body.model_dump()}
    )
    with Session(engine) as db:
        try:
            request = TrackingRequest(
                actor=actor,
                request_key=body.request_key,
                payload_json=payload,
                result_json="",
            )
            db.add(request)
            db.flush()
            now = datetime.now(UTC)
            if isinstance(body, Create):
                if item_id is not None:
                    raise TrackingError("create does not take an item id")
                item = WorkItem(
                    **body.model_dump(exclude={"request_key"}),
                    execution_mode="tracking_only",
                    source_kind="mcp",
                    authority="local",
                    trust="untrusted",
                    state="open",
                    tracking_version=1,
                    created_at=now,
                    updated_at=now,
                )
                db.add(item)
                db.flush()
                changes = document(item)
            else:
                item = db.get(WorkItem, item_id)
                if item is None or item.execution_mode != "tracking_only":
                    raise TrackingError("tracking item not found")
                if item.tracking_version != body.expected_version:
                    raise TrackingError("stale expected_version")
                old = item.state
                legal = (
                    body.state == old
                    or (old in _OPEN and body.state in (*_OPEN, "done", "closed"))
                    or (old in ("done", "closed") and body.state == "open")
                )
                if not legal:
                    raise TrackingError("illegal tracking transition; reopen to open")
                if (
                    body.state == "done"
                    and old != "done"
                    and not body.completion_evidence
                ):
                    raise TrackingError("completion requires evidence")
                if body.completion_evidence is not None and body.state != "done":
                    raise TrackingError("completion evidence requires done")
                changes = body.model_dump(
                    exclude={"request_key", "expected_version"}, exclude_none=True
                )
                changes.update(
                    tracking_version=body.expected_version + 1,
                    updated_at=now,
                    close_reason="not_planned" if body.state == "closed" else None,
                    closed_at=now if body.state == "closed" else None,
                )
                if body.state == "done" and old != "done":
                    changes["completed_at"] = now
                # Reopen preserves the last completion and its attributable event.
                changed = db.execute(
                    update(WorkItem)
                    .where(
                        WorkItem.id == item_id,
                        WorkItem.execution_mode == "tracking_only",
                        WorkItem.tracking_version == body.expected_version,
                    )
                    .values(**changes)
                )
                if changed.rowcount != 1:
                    raise TrackingError("stale expected_version")
                db.refresh(item)
            result = {"ok": True, "item": document(item)}
            db.add(
                WorkItemEvent(
                    work_item_id=item.id,
                    version=item.tracking_version,
                    op="tracking_create"
                    if isinstance(body, Create)
                    else "tracking_update",
                    author_kind="operator",
                    author=actor,
                    change_json=_json(result["item"]),
                    cause_kind="tracking_request",
                    cause_ref=body.request_key,
                )
            )
            request.result_json = _json(result)
            db.commit()
            return result
        except IntegrityError as exc:
            db.rollback()
            previous = db.exec(
                select(TrackingRequest).where(
                    TrackingRequest.actor == actor,
                    TrackingRequest.request_key == body.request_key,
                )
            ).one_or_none()
            if previous is not None:
                if previous.payload_json != payload:
                    raise TrackingError(
                        "request_key reused with different payload"
                    ) from exc
                return json.loads(previous.result_json)
            raise TrackingError(
                "source identity already recorded or storage constraint violated"
            ) from exc


def read(engine, item_id: int) -> dict:
    with Session(engine) as db:
        item = db.get(WorkItem, item_id)
        if item is None or item.execution_mode != "tracking_only":
            raise TrackingError("tracking item not found")
        events = db.exec(
            select(WorkItemEvent)
            .where(WorkItemEvent.work_item_id == item_id)
            .order_by(WorkItemEvent.version.desc())
            .limit(20)
        ).all()
        return {
            "item": document(item),
            "events": [
                {
                    "version": event.version,
                    "author": event.author,
                    "op": event.op,
                    "change_json": event.change_json,
                    "created_at": aware_iso(event.created_at),
                }
                for event in events
            ],
        }


def listing(
    engine,
    *,
    state: State | None = None,
    tracking_kind: Literal["manual", "external"] | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise TrackingError("limit must be 1..100")
    if state is not None and state not in (*_OPEN, "done", "closed"):
        raise TrackingError("invalid state")
    if tracking_kind not in (None, "manual", "external"):
        raise TrackingError("invalid tracking_kind")
    # Cursor binds filters to the last durable ID; no offsets or encoded SQL.
    scope = f"{state or '*'}:{tracking_kind or '*'}:"
    before = None
    if cursor is not None:
        if (
            not isinstance(cursor, str)
            or len(cursor) > 100
            or not cursor.startswith(scope)
        ):
            raise TrackingError("invalid cursor")
        tail = cursor[len(scope) :]
        if (
            not tail.isascii()
            or not tail.isdecimal()
            or not 1 <= int(tail) <= 2**63 - 1
        ):
            raise TrackingError("invalid cursor")
        before = int(tail)
    query = select(WorkItem).where(WorkItem.execution_mode == "tracking_only")
    if state is not None:
        query = query.where(WorkItem.state == state)
    if tracking_kind is not None:
        query = query.where(WorkItem.tracking_kind == tracking_kind)
    if before is not None:
        query = query.where(WorkItem.id < before)
    with Session(engine) as db:
        rows = db.exec(query.order_by(WorkItem.id.desc()).limit(limit + 1)).all()
        return {
            "items": [document(row) for row in rows[:limit]],
            "next_cursor": scope + str(rows[limit - 1].id)
            if len(rows) > limit
            else None,
        }
