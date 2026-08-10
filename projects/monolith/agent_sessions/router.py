from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, func, select

from agent_sessions import model_family, store
from agent_sessions.codex_login import codex_login_gate, watch_for_login
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.mcp import (
    _clear_ember_bindings_for,
    _load_session_row,
    _mark_ui_originated,
    _persist_pending_message,
    _persist_session,
    _schedule_next_message,
    _set_session_status,
    _transport,
)
from core.db import get_session
from faas.embervm_client import EmberVMTransportError
from goosecracker.api import REPO_CATALOG

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agents", tags=["agents"])
_DEFAULT_BRANCH_CACHE: dict[str, tuple[float, str | None]] = {}
_REPO_CACHE_TTL = 300.0
_REPO_CACHE_FAILURE_TTL = 30.0


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _as_utc(value)
    return value.isoformat() if value is not None else None


def _decode(value: str | None, default):
    if not value:
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return decoded


def _aggregate_statement(status: str | None = None, session_id: int | None = None):
    turns_statement = select(
        AgentTurn.session_id,
        func.count(AgentTurn.id).label("turn_count"),
        func.coalesce(func.sum(AgentTurn.cost_usd), 0).label("total_cost_usd"),
    )
    if session_id is not None:
        turns_statement = turns_statement.where(AgentTurn.session_id == session_id)
    turns = turns_statement.group_by(AgentTurn.session_id).subquery()

    pending_statement = select(
        PendingMessage.session_id,
        func.count(PendingMessage.id).label("pending_count"),
    )
    if session_id is not None:
        pending_statement = pending_statement.where(
            PendingMessage.session_id == session_id
        )
    pending = pending_statement.group_by(PendingMessage.session_id).subquery()
    # The session list renders a human title (the first prompt) instead of
    # the raw UUID; sessions whose first turn has not completed yet fall
    # back to the first queued prompt. Truncated in SQL: the list is polled
    # every 2s while a session is active, and _fallback_title only keeps
    # 140 chars, so shipping whole prompt TEXT columns would be waste.
    first_turn_prompt = (
        select(func.substr(AgentTurn.prompt, 1, 200))
        .where(AgentTurn.session_id == AgentSession.id)
        .order_by(AgentTurn.seq)
        .limit(1)
        .scalar_subquery()
    )
    first_pending_prompt = (
        select(func.substr(PendingMessage.message_text, 1, 200))
        .where(PendingMessage.session_id == AgentSession.id)
        .order_by(PendingMessage.seq)
        .limit(1)
        .scalar_subquery()
    )
    statement = (
        select(
            AgentSession,
            func.coalesce(turns.c.turn_count, 0),
            func.coalesce(turns.c.total_cost_usd, 0),
            func.coalesce(pending.c.pending_count, 0),
            first_turn_prompt,
            first_pending_prompt,
        )
        .outerjoin(turns, turns.c.session_id == AgentSession.id)
        .outerjoin(pending, pending.c.session_id == AgentSession.id)
        .order_by(AgentSession.last_turn_at.desc())
    )
    if status is not None:
        statement = statement.where(AgentSession.status == status)
    return statement


def _fallback_title(
    first_turn_prompt: str | None, first_pending_prompt: str | None
) -> str:
    text = (first_turn_prompt or first_pending_prompt or "").strip()
    return text.split("\n")[0][:140]


def _session_payload(
    row: AgentSession,
    turn_count: int,
    total_cost_usd: float,
    pending_count: int,
    first_turn_prompt: str | None = None,
    first_pending_prompt: str | None = None,
) -> dict:
    return {
        "id": row.id,
        "local_session_id": row.local_session_id,
        "workspace": row.workspace,
        "branch": row.branch,
        "repo": row.repo,
        "workflow_id": row.workflow_id,
        "node_key": row.node_key,
        "node_attempt": row.node_attempt,
        "triggered_by": row.triggered_by,
        "model": row.model,
        "status": row.status,
        "title": row.title or _fallback_title(first_turn_prompt, first_pending_prompt),
        "ember_session_id": row.ember_session_id,
        "created_at": _iso(row.created_at),
        "last_turn_at": _iso(row.last_turn_at),
        "voice_summary": row.voice_summary,
        "turn_count": int(turn_count),
        "total_cost_usd": float(total_cost_usd or 0),
        "pending_count": int(pending_count),
    }


def _rows(session: Session, status: str | None = None, limit: int | None = None):
    statement = _aggregate_statement(status, None)
    if limit is not None:
        statement = statement.limit(limit)
    results = session.exec(statement).all()
    return [_session_payload(*result) for result in results]


class StartRequest(BaseModel):
    prompt: str
    model: str | None = None
    workspace: str = "<guest>"
    branch: str = "main"
    repo: str | None = None


class MessageRequest(BaseModel):
    prompt: str
    model: str | None = None


@router.get("/sessions")
def list_sessions(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    return _rows(session, status, limit)


# Control-plane session states collapsed to what the console renders. The
# guest lifecycle is the control plane's truth, not the monolith's local
# binding: a park (idleBankSeconds after the last invoke) happens without
# the monolith noticing until the next send, so the UI polls this instead.
_VM_AWAKE_STATES = {"creating", "running", "relighting"}
_VM_ASLEEP_STATES = {"banking", "banked", "parking", "parked"}


class _VmStateCache:
    refresh_interval = 1.0
    heartbeat_interval = 15.0

    def __init__(self) -> None:
        self.cache_map: dict[str, dict] = {}
        self.last_refreshed_at = 0.0
        self.subscriber_count = 0
        self.task: asyncio.Task | None = None
        self.change_event: asyncio.Event | None = None
        self.generation = 0
        self.initialized = False
        self.last_error: str | None = None


_vm_state_cache = _VmStateCache()


def _coarse_vm_map(items: list[dict]) -> dict[str, dict]:
    vms = {}
    for item in items:
        state = str(item.get("state", ""))
        if state in _VM_AWAKE_STATES:
            coarse = "awake"
        elif state in _VM_ASLEEP_STATES:
            coarse = "asleep"
        else:
            coarse = "off"
        vms[item.get("session_id")] = {
            "state": coarse,
            "cp_state": state,
            "last_invoke_at": item.get("last_invoke_at"),
            "expires_at": item.get("expires_at"),
        }
    return vms


async def _refresh_cp_state() -> None:
    items = []
    offset = 0
    try:
        for _ in range(4):
            page = await _transport.list_sessions(limit=500, offset=offset)
            batch = page.get("items", [])
            items.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page.get("total") or 0):
                break
    except EmberVMTransportError as exc:
        # Drop the map rather than serving the last known one. A stale entry
        # renders as a confident "awake" chip for a guest that may be long
        # gone, where an absent entry renders "off" alongside the error, which
        # is what this endpoint promised before it was cached. Losing the CP
        # means we do not know, and the honest rendering of not knowing is the
        # one the console already had.
        logger.warning("failed to refresh agent VM state: %s", exc)
        _publish_vm_map({}, error=str(exc))
        return

    _publish_vm_map(_coarse_vm_map(items), error=None)


def _publish_vm_map(new_map: dict[str, dict], error: str | None) -> None:
    """Store a map and wake subscribers only when it actually differs.

    The generation counter, rather than a bare Event, is what lets each
    subscriber tell whether it already saw a change: a single shared Event
    cannot distinguish "woke for this change" from "woke for the previous
    one", so a client waking between set and clear would double-send or miss
    an update entirely.
    """
    cache = _vm_state_cache
    # The error is part of what changed, not just the map. With no live
    # sessions the map is already empty, so losing the control plane
    # published an identical {} and woke nobody: subscribers kept a frame
    # claiming a clean empty state while the snapshot endpoint reported the
    # outage. Recovery was equally silent.
    changed = new_map != cache.cache_map or error != cache.last_error
    cache.cache_map = new_map
    cache.last_refreshed_at = time.monotonic()
    cache.last_error = error
    cache.initialized = True
    if not changed:
        return
    cache.generation += 1
    old_event = cache.change_event
    cache.change_event = asyncio.Event()
    if old_event is not None:
        old_event.set()


async def _run_vm_refresher() -> None:
    while True:
        try:
            await _refresh_cp_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad poll must not end the loop
            # A dead refresher is invisible: subscribers keep receiving the 15s
            # heartbeat so the connection looks healthy, no message ever
            # arrives so the client's fallback never engages, and the snapshot
            # endpoint sees a non-None task and stops refreshing too. The chips
            # freeze and every surface reports fine. Publishing the outage is
            # what makes the failure look like a failure.
            logger.exception("agent VM refresher iteration failed")
            _publish_vm_map({}, error=str(exc))
        await asyncio.sleep(_vm_state_cache.refresh_interval)


async def _start_refresher() -> None:
    cache = _vm_state_cache
    cache.subscriber_count += 1
    if cache.change_event is None:
        cache.change_event = asyncio.Event()
    # Create the task BEFORE awaiting anything. With the await first, the
    # check and the set straddled a suspension point: two subscribers
    # connecting together both saw task is None and both assigned, so the
    # first task lost its only reference. Unreachable, uncancellable, and
    # polling the control plane once a second until the pod restarts.
    # Deployment is 1 replica with HPA to 3, so worst case is 3 refreshers.
    if cache.task is None or cache.task.done():
        cache.task = asyncio.create_task(_run_vm_refresher())
    # Give a joining subscriber fresh data before it yields its snapshot.
    # Staleness, not just initialization: `initialized` latches true on the
    # first publish and never resets, so testing it alone meant a console
    # reopened hours after the last viewer left got a frame built from the
    # overnight map, rendering a confident "awake" chip for a guest that
    # parked long ago. Costs nothing while a refresher is live, since
    # last_refreshed_at is then always under one interval old.
    stale = time.monotonic() - cache.last_refreshed_at >= cache.refresh_interval
    if not cache.initialized or stale:
        await _refresh_cp_state()


async def _stop_refresher() -> None:
    cache = _vm_state_cache
    cache.subscriber_count = max(0, cache.subscriber_count - 1)
    if cache.subscriber_count or cache.task is None:
        return
    task, cache.task = cache.task, None
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _vm_frame() -> str:
    """One SSE data frame, carrying the same payload as the snapshot endpoint.

    Kept identical to `/vms` on purpose: two surfaces describing the same
    state with different fields is how a client ends up unable to tell an
    empty map from an outage on one of them.
    """
    body: dict = {"vms": _vm_state_cache.cache_map}
    if _vm_state_cache.last_error:
        body["error"] = _vm_state_cache.last_error
    return f"data: {json.dumps(body)}\n\n"


@router.get("/vms/stream")
async def stream_session_vms() -> StreamingResponse:
    async def generate():
        # Inside the try, not before it. _start_refresher increments the
        # subscriber count as its first statement and then awaits, and
        # Starlette cancels this generator at its innermost await on client
        # disconnect. Started outside, a client that vanished during that
        # first refresh (a wedged control plane can hold it ~20s) left the
        # count permanently above zero, so every later stop returned early
        # and the refresher polled forever with nobody watching.
        cache = _vm_state_cache
        try:
            await _start_refresher()
            last_generation = cache.generation
            yield _vm_frame()
            while True:
                if cache.generation != last_generation:
                    last_generation = cache.generation
                    yield _vm_frame()
                    continue
                event = cache.change_event
                try:
                    await asyncio.wait_for(
                        event.wait(), timeout=cache.heartbeat_interval
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if cache.generation != last_generation:
                    last_generation = cache.generation
                    yield _vm_frame()
        finally:
            await _stop_refresher()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/vms")
async def list_session_vms() -> dict:
    """Return the shared VM snapshot, refreshing it when nothing is feeding it.

    This is the console's fallback for a broken event stream, so it must NOT
    depend on a stream subscriber having connected first. With no refresher
    running, a cache-only read stays empty forever and the chip reads "off"
    for every session, which is the opposite of the truth and precisely the
    case the fallback exists to cover.

    The refresh is skipped while a refresher owns the cache, and rate limited
    to the same interval otherwise, so a fallback poll cannot reintroduce
    per-request control-plane load.
    """
    cache = _vm_state_cache
    stale = time.monotonic() - cache.last_refreshed_at >= cache.refresh_interval
    # `task.done()` matters as much as `task is None`: a refresher that died
    # is not None, so testing only for None let a crashed task freeze this
    # endpoint too, and the fallback stopped falling back.
    owned = cache.task is not None and not cache.task.done()
    if not owned and (not cache.initialized or stale):
        await _refresh_cp_state()
    body = {"vms": cache.cache_map}
    if cache.last_error:
        body["error"] = cache.last_error
    return body


@router.get("/sessions/{session_id}")
def get_session_detail(
    session_id: int,
    after_seq: int | None = Query(default=None, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    result = session.exec(
        _aggregate_statement(session_id=session_id).where(AgentSession.id == session_id)
    ).first()
    if result is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    row, turn_count, total_cost_usd, pending_count, first_turn, first_pending = result
    turns_statement = select(AgentTurn).where(AgentTurn.session_id == session_id)
    if after_seq is not None:
        turns_statement = turns_statement.where(AgentTurn.seq > after_seq)
    turns = session.exec(turns_statement.order_by(AgentTurn.seq)).all()
    pending = session.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == session_id)
        .order_by(PendingMessage.seq)
    ).all()
    return {
        "session": _session_payload(
            row, turn_count, total_cost_usd, pending_count, first_turn, first_pending
        ),
        "turns": [
            {
                "seq": turn.seq,
                "prompt": turn.prompt,
                "model": turn.model,
                "result_text": turn.result_text,
                "voice_summary": turn.voice_summary,
                "terminal_reason": turn.terminal_reason,
                "stop_reason": turn.stop_reason,
                "permission_denials": _decode(turn.permission_denials, []),
                "commit_sha": turn.commit_sha,
                "usage": _decode(turn.usage_json, {}),
                "cost_usd": turn.cost_usd,
                "created_at": _iso(turn.created_at),
            }
            for turn in turns
        ],
        "pending_queue": [
            {
                "seq": message.seq,
                "prompt": message.message_text,
                "partial_text": message.partial_text,
                "partial_activities": _decode(message.partial_activities, None),
                "claimed_by_replica": message.claimed_by_replica,
                "claimed_at": _iso(message.claimed_at),
                "created_at": _iso(message.created_at),
            }
            for message in pending
        ],
    }


@router.post("/sessions")
async def start_session(request: Request, start_request: StartRequest) -> dict:
    triggered_by = request.headers.get("x-auth-email")
    triggered_by = triggered_by.strip().lower() or None if triggered_by else None
    if start_request.repo is not None and start_request.repo not in REPO_CATALOG:
        return {
            "accepted": False,
            "error": (
                f"unknown repo {start_request.repo}; catalog: {', '.join(REPO_CATALOG)}"
            ),
        }
    try:
        model_family(start_request.model)
    except ValueError as exc:
        return {"accepted": False, "error": str(exc)}
    row = await asyncio.to_thread(
        _persist_session,
        str(uuid4()),
        start_request.workspace,
        start_request.branch,
        start_request.model,
        start_request.repo,
        triggered_by=triggered_by,
    )
    turn = await asyncio.to_thread(
        _persist_pending_message, row.id, start_request.prompt, start_request.model
    )
    # Queued from the UI, so its result does not get echoed to Discord.
    _mark_ui_originated(row.id, turn)
    login = await codex_login_gate(start_request.model)
    if login is not None:

        async def resume() -> None:
            await asyncio.to_thread(_set_session_status, row.id, "running")
            _schedule_next_message(row.id)

        watch_for_login(login.get("grant", "codex-cluster"), resume)
        return {"accepted": False, **login, "session_id": row.id, "turn": turn}
    _schedule_next_message(row.id)
    return {"accepted": True, "session_id": row.id, "turn": turn}


async def _github_get(url: str) -> httpx.Response:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response


@router.get("/repos")
async def list_repos() -> dict:
    now = time.monotonic()
    uncached = []
    for repo_id in REPO_CATALOG:
        cached = _DEFAULT_BRANCH_CACHE.get(repo_id)
        if cached is None or cached[0] <= now:
            uncached.append(repo_id)

    async def fetch_default_branch(repo_id: str) -> None:
        try:
            response = await _github_get(f"https://api.github.com/repos/{repo_id}")
            default_branch = response.json().get("default_branch")
            if not isinstance(default_branch, str):
                default_branch = None
        except Exception:
            _DEFAULT_BRANCH_CACHE[repo_id] = (
                time.monotonic() + _REPO_CACHE_FAILURE_TTL,
                None,
            )
            return
        _DEFAULT_BRANCH_CACHE[repo_id] = (
            time.monotonic() + _REPO_CACHE_TTL,
            default_branch,
        )

    await asyncio.gather(*(fetch_default_branch(repo_id) for repo_id in uncached))

    repos = []
    for repo_id, entry in REPO_CATALOG.items():
        cached = _DEFAULT_BRANCH_CACHE.get(repo_id)
        default_branch = cached[1] if cached is not None else None
        repos.append(
            {
                "id": repo_id,
                "description": entry.description,
                "default_branch": default_branch,
            }
        )
    return {"repos": repos}


@router.get("/repos/{owner}/{repo}/branches")
async def list_repo_branches(owner: str, repo: str) -> dict:
    repo_id = f"{owner}/{repo}"
    if repo_id not in REPO_CATALOG:
        raise HTTPException(status_code=404, detail="Repository not in catalog")
    if not os.environ.get("GITHUB_API_TOKEN"):
        raise HTTPException(
            status_code=503,
            detail="GITHUB_API_TOKEN is not set",
        )
    try:
        repo_response = await _github_get(f"https://api.github.com/repos/{repo_id}")
        default_branch = repo_response.json().get("default_branch")
        if not isinstance(default_branch, str):
            default_branch = None
        branches = []
        branches_url = f"https://api.github.com/repos/{repo_id}/branches?per_page=100"
        for _ in range(10):
            branches_response = await _github_get(branches_url)
            page = branches_response.json()
            if not isinstance(page, list):
                raise HTTPException(
                    status_code=502, detail="GitHub branches response was not a list"
                )
            branches.extend(
                {"name": branch["name"]}
                for branch in page
                if isinstance(branch, dict) and isinstance(branch.get("name"), str)
            )
            next_link = branches_response.links.get("next")
            if not next_link:
                break
            branches_url = next_link["url"]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub API request failed: {exc}"
        ) from exc
    branches.sort(key=lambda branch: (branch["name"] != default_branch, branch["name"]))
    return {"branches": branches, "default_branch": default_branch}


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: int, request: MessageRequest) -> dict:
    row = await asyncio.to_thread(_load_session_row, session_id)
    if row is None:
        return {"accepted": False, "error": f"Unknown agent session {session_id}"}
    try:
        session_family = model_family(row.model)
        requested_family = (
            model_family(request.model) if request.model is not None else session_family
        )
    except ValueError as exc:
        return {"accepted": False, "error": str(exc)}
    if requested_family != session_family:
        return {
            "accepted": False,
            "error": (
                f"Model family mismatch: session family is {session_family}, "
                f"requested model family is {requested_family}"
            ),
        }
    effective_model = request.model or row.model
    login = await codex_login_gate(effective_model)
    if login is not None:
        return {"accepted": False, **login}
    turn = await asyncio.to_thread(
        _persist_pending_message, session_id, request.prompt, effective_model
    )
    # Queued from the UI, so its result does not get echoed to Discord.
    _mark_ui_originated(session_id, turn)
    await asyncio.to_thread(_set_session_status, session_id, "running")
    _schedule_next_message(session_id)
    return {"accepted": True, "session_id": session_id, "turn": turn}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int) -> dict:
    try:
        row = await asyncio.to_thread(_load_session_row, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Agent session not found")
        if row.ember_session_id is None:
            return {}
        result = await _transport.destroy_session(row.ember_session_id)
        result["cleared_bindings"] = await asyncio.to_thread(
            _clear_ember_bindings_for, row.ember_session_id
        )
        return result
    except EmberVMTransportError as exc:
        return {"error": str(exc)}


@router.get("/search")
def search_sessions(
    q: str = Query(...),
    limit: int = Query(default=20, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict:
    results = store.lexical_search(session, q, limit)
    return {
        "results": [
            {
                "session_id": result["session_id"],
                "local_session_id": result["local_session_id"],
                "workspace": result["workspace"],
                "seq": result["seq"],
                "created_at": _iso(result["created_at"]),
                "rank": float(result["rank"]),
                "snippet": result["snippet"],
            }
            for result in results
        ]
    }
