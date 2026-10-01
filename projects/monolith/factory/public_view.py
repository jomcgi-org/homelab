"""Public, aggregate-only agent activity API, plus the read-only factory pages.

Every route reads a public_api snapshot table and nothing else: the activity
route public_api.agent_activity_snapshot, the factory routes
public_api.factory_*_snapshot. Those rows are built on the private side by factory/publication.py, because
public_reader has no grant on the swarm or agent_sessions schemas and private
factory execution code is not in the public image.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from core.db import get_session
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents/public", tags=["agents-public"])

_ACTIVITY_CACHE_CONTROL = "public, max-age=300, s-maxage=300"
# The factory board moves on a 2-minute snapshot cadence, so a 60s shared cache
# never serves anything the writer has not had a chance to refresh, while still
# absorbing a burst of readers. FACTORY_ACTIVITY_CACHE_CONTROL in
# frontend/src/lib/cache-headers.js mirrors this; keep the two in sync.
_FACTORY_CACHE_CONTROL = "public, max-age=60, s-maxage=60"
# One row, written by the private-tier agent-activity-snapshot job from
# factory/publication.py build_agent_activity. Aggregating here instead parsed
# every turn's usage_json per request (4 to 6 seconds).
_AGENT_ACTIVITY_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.agent_activity_snapshot
    WHERE id = 1
    """
)


def _activity_etag(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return f'"agent-activity-v2-{digest}"'


@router.get("/activity")
def get_public_agent_activity(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Return identity-free agent activity aggregates, every cost at list price."""
    try:
        row = session.execute(_AGENT_ACTIVITY_QUERY).first()
    except SQLAlchemyError as exc:
        logger.warning("public.agent_activity.unavailable", exc_info=exc)
        raise HTTPException(
            status_code=500, detail="agent activity unavailable"
        ) from exc
    if row is None:
        raise HTTPException(status_code=503, detail="agent activity not snapshotted")

    payload = _payload(row)
    etag = _activity_etag(payload)
    headers = {"Cache-Control": _ACTIVITY_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return payload


_FACTORY_ACTIVITY_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.factory_activity_snapshot
    WHERE id = 1
    """
)
_FACTORY_TASK_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.factory_task_snapshot
    WHERE issue_number = :issue_number
    """
)
_FACTORY_SESSION_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.factory_session_snapshot
    WHERE session_key = :session_key
    """
)
_FACTORY_WORK_ITEM_QUERY = text(
    """
    SELECT payload, snapshotted_at
    FROM public_api.factory_work_item_snapshot
    WHERE work_item_id = :work_item_id
    """
)


def _value(row: Any, name: str) -> Any:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping[name]
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def _payload(row: Any) -> dict:
    """JSONB comes back decoded on psycopg; tolerate a driver that returns text."""
    payload = _value(row, "payload")
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return payload


def _factory_etag(kind: str, payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f'"factory-{kind}-v1-{hashlib.sha256(encoded).hexdigest()}"'


def _factory_response(
    request: Request,
    response: Response,
    kind: str,
    payload: dict,
):
    """Serve one snapshot payload with its ETag, 304-ing an unchanged read."""
    etag = _factory_etag(kind, payload)
    headers = {"Cache-Control": _FACTORY_CACHE_CONTROL, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    return payload


def _factory_row(session: Session, query, params: dict | None = None) -> Any:
    try:
        return session.execute(query, params or {}).first()
    except SQLAlchemyError as exc:
        logger.warning("public.factory_snapshot.unavailable", exc_info=exc)
        raise HTTPException(status_code=500, detail="factory unavailable") from exc


@router.get("/factory/activity")
def get_public_factory_activity(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """The factory board: what is in flight, what is queued, what recently ran."""
    row = _factory_row(session, _FACTORY_ACTIVITY_QUERY)
    if row is None:
        raise HTTPException(status_code=404, detail="no snapshot")
    return _factory_response(request, response, "activity", _payload(row))


@router.get("/factory/tasks/{issue_number}")
def get_public_factory_task(
    issue_number: int,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """One task's walkthrough: the brief, the plan, and every attempt's turns."""
    row = _factory_row(session, _FACTORY_TASK_QUERY, {"issue_number": issue_number})
    if row is None:
        raise HTTPException(status_code=404, detail="unknown task")
    return _factory_response(request, response, "task", _payload(row))


@router.get("/factory/work-items/{work_item_id}")
def get_public_factory_work_item(
    work_item_id: int,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """One safe work-item snapshot, with no read access to private factory rows."""
    row = _factory_row(
        session, _FACTORY_WORK_ITEM_QUERY, {"work_item_id": work_item_id}
    )
    if row is None:
        raise HTTPException(status_code=404, detail="unknown work item")
    return _factory_response(request, response, "work-item", _payload(row))


# ``:path`` because a session key is factory:<task_id>:<node_key>:<attempt> and
# a node key may itself contain colons. The key is opaque here: the snapshot
# writer supplies it and this route only looks it up.
@router.get("/factory/sessions/{session_key:path}")
def get_public_factory_session(
    session_key: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """One attempt's full record: every turn with its diff, rationale and usage."""
    row = _factory_row(session, _FACTORY_SESSION_QUERY, {"session_key": session_key})
    if row is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return _factory_response(request, response, "session", _payload(row))
