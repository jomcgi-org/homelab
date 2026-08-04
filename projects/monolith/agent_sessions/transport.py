"""EmberVM shim transport for Claude Code session execution.

The Claude Code CLI runs in an EmberVM guest, not in the monolith pod.
The guest exposes a session API through the EmberVM control plane.

Reuses the patterns from faas/embervm_client.py (httpx.AsyncClient,
EMBERVM_URL, auth headers, error types, timeout shape).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, NamedTuple, Protocol

import httpx

from faas.embervm_client import (
    EmberVMTimeout,
    EmberVMTransportError,
    SUBMIT_CONNECT_TIMEOUT,
)
from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)


def _retryable_from_response(exc: httpx.HTTPStatusError) -> bool:
    try:
        body = exc.response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return error.get("retryable") is True
        return isinstance(body, dict) and body.get("retryable") is True
    except Exception:
        return False


async def _invoke_with_retryable_backoff(
    invoke_fn: Callable[[], Awaitable["Turn"]],
    max_attempts: int = 4,
) -> "Turn":
    """Call invoke_fn with exponential backoff on retryable errors."""
    backoff_seconds = [2, 5, 10]
    for attempt in range(max_attempts):
        try:
            return await invoke_fn()
        except httpx.HTTPStatusError as exc:
            if attempt == max_attempts - 1 or not _retryable_from_response(exc):
                raise
            await asyncio.sleep(backoff_seconds[attempt])
    raise AssertionError("retry loop did not return or raise")


def _status_error_detail(exc: httpx.HTTPStatusError) -> str:
    """The status line PLUS a bounded slice of the response body.

    The guest shim reports WHY a turn failed in the error response body
    (e.g. pi's provider errorMessage); persisting only the httpx status
    line hid every root cause behind a bare 422 (#4252).
    """
    detail = str(exc)
    try:
        body = exc.response.text.strip()
    except Exception:  # noqa: BLE001 - body read must never mask the error
        body = ""
    if body:
        detail += "\nresponse body: " + body[:1500]
    return detail


# The control plane base URL, set on the monolith deployment (chart
# templates/deployment.yaml). Read at import exactly like
# faas/embervm_client.py does, so both clients resolve it the same way. The
# empty default is what the guards below check: unset means the guest lane is
# not reachable, not that it is reachable at the empty URL.
EMBERVM_URL = os.environ.get("EMBERVM_URL", "")


class EmberSessionGone(EmberVMTransportError):
    """Raised when a reused EmberVM session is confirmed dead by the CP (403/410)
    and the retry also failed. The ORIGINAL binding is dead regardless of retry failure reason."""

    pass


class Turn(NamedTuple):
    result: str
    terminal_reason: str | None
    stop_reason: str | None
    is_error: bool
    permission_denials: list
    num_turns: int
    session_id: str | None
    usage: dict
    total_cost_usd: float | None
    duration_ms: int | None
    activities: list[dict]
    workspace_recovery: dict | None = None


class EmberSession(NamedTuple):
    session_id: str
    session_token: str
    expires_at: int | None
    # #4306 slice 4: lineage_id is the durable workspace handle to pass as
    # restore_from on a LATER create to continue this same lineage. It
    # equals session_id for a normal (gen-0) create; a RESTORED session
    # mints a fresh session_id but inherits the prior lineage_id, so the two
    # diverge (session_id is NOT a valid restore key past generation zero).
    # restored is True only when a restore_from create actually recovered
    # the workspace (never set for a normal create, and never set on a
    # denied restore, which raises instead of returning a session at all).
    lineage_id: str | None = None
    restored: bool = False


class ShimTransport(Protocol):
    async def deliver(
        self,
        ember: EmberSession | None,
        cli_session_id: str | None,
        message: str,
        model: str | None = None,
        restore_from: str | None = None,
        on_create: Callable[[EmberSession], Awaitable[None]] | None = None,
    ) -> tuple[Turn, EmberSession]: ...


# Read timeout for invoke calls: the OUTER (wall-clock) bound on turn duration.
# The guest shim (projects/embervm/runtimes/claude/shim.py) enforces an INNER
# inactivity timeout (TURN_READ_TIMEOUT, per-event), which fires fast if the
# CLI wedges. This outer cap must be comfortably larger so the inner watchdog
# can fire first and catch transient hangs; this value catches runaway turns
# that produce output continuously but never terminate. The heartbeat on the
# monolith side (claim refresh every 10s against 30s lease) ensures a turn
# running for the full duration keeps its claim and is never double-executed.


class EmberVmShimTransport:
    """HTTP client transport for the Claude runtime guest over EmberVM control plane.

    The guest runs the Claude Code CLI as a session-based REPL, answering over
    the /shim/turn endpoint. This transport orchestrates session creation and
    invocation via the control plane's /v1/sessions/:id/invoke API.
    """

    def __init__(
        self,
        workload: str = "claude-runtime",
        read_timeout: float = 1800.0,
    ) -> None:
        """Initialize transport for a named EmberVM workload.

        Args:
            workload: Name of the EmberVM workload (e.g., 'claude-runtime').
            read_timeout: Total wall-clock duration cap for a single turn
                (seconds). This is the OUTER bound: the maximum time allowed
                for the entire turn regardless of output activity. The guest
                shim enforces an inner inactivity timeout (per-event), which
                catches wedged CLIs quickly; this outer bound stops runaway
                turns that produce output continuously. Must be comfortably
                larger than the guest's inactivity timeout so the inner
                watchdog can fire first (default 1800s = 30 minutes).
        """
        self.workload = workload
        self.read_timeout = read_timeout

    async def create_session(
        self, restore_from: str | None = None, _attempt: int = 0
    ) -> EmberSession:
        """Create a new session on the guest and return the session ID.

        Args:
            restore_from: A prior LINEAGE handle (EmberSession.lineage_id,
                not necessarily session_id past generation zero) to inherit
                the prior generation's guest workspace from (#4306 slice 4:
                cross-generation continuity). None (the default) requests a
                normal, blank create.

        Returns:
            The EmberVM session identity and bearer token. lineage_id is the
            durable workspace handle to pass as restore_from on a LATER
            create to continue this same lineage (== session_id for a
            normal create). restored is True only when restore_from was
            actually honored (the workspace was recovered); a denied
            restore raises below like any other create failure, it never
            returns restored=False for "denied".

        Raises:
            EmberVMTransportError: If session creation fails, including a
                restore denial (unknown lineage, a workload/principal
                mismatch, a live heir, or a concurrent in-flight restore of
                the same lineage; see the control plane's create_denial
                mapping, #4306 slice 3).
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        url = f"{EMBERVM_URL}/v1/workloads/{self.workload}/sessions"
        headers = auth_headers()
        timeout = httpx.Timeout(self.read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)
        # A normal create posts no body at all: the CP's create route parses
        # an OPTIONAL JSON body, and an absent one is exactly what it treats
        # as "no restore requested" (see optional_restore_lineage in the CP
        # router), so this stays a plain no-body POST unless restoring.
        post_kwargs = {"headers": headers}
        if restore_from:
            post_kwargs["json"] = {"restore_lineage": restore_from}

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, **post_kwargs)
                response.raise_for_status()
                data = response.json()
                session_id = data.get("session_id")
                if not session_id:
                    raise EmberVMTransportError(
                        "EmberVM session response missing session_id"
                    )
                session_token = data.get("session_token")
                if not session_token:
                    raise EmberVMTransportError(
                        "EmberVM session response missing session_token"
                    )
                return EmberSession(
                    session_id=session_id,
                    session_token=session_token,
                    expires_at=data.get("expires_at"),
                    lineage_id=data.get("lineage_id"),
                    restored=bool(data.get("restored", False)),
                )
        except httpx.TimeoutException as exc:
            logger.warning("embervm session creation timed out: %s", exc)
            raise EmberVMTimeout(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("embervm session creation failed: %s", exc)
            if _attempt < 3 and _retryable_from_response(exc):
                await asyncio.sleep((2, 5, 10)[_attempt])
                return await self.create_session(restore_from, _attempt + 1)
            raise EmberVMTransportError(_status_error_detail(exc)) from exc
        except httpx.TransportError as exc:
            logger.warning("embervm session creation transport error: %s", exc)
            raise EmberVMTransportError(str(exc)) from exc

    # The operator surface for the workload cap: parked sessions count as live
    # slot holders, so a stale test session denies every new create until it
    # is listed here and destroyed. Both calls use management auth, not a
    # session bearer token, so they act on any session in the workload.

    async def list_sessions(self, limit: int = 50, offset: int = 0) -> dict:
        """List the control plane's sessions for this workload (management auth).

        Args:
            limit: Maximum sessions to return (clamped 1-500 server side).
            offset: Offset into the workload's session list.

        Returns:
            The control plane's paginated session listing.

        Raises:
            EmberVMTransportError: If the listing request fails.
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        url = f"{EMBERVM_URL}/v1/workloads/{self.workload}/sessions"
        headers = auth_headers()
        timeout = httpx.Timeout(self.read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    url, params={"limit": limit, "offset": offset}, headers=headers
                )
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException as exc:
            logger.warning("embervm session listing timed out: %s", exc)
            raise EmberVMTimeout(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("embervm session listing failed: %s", exc)
            raise EmberVMTransportError(_status_error_detail(exc)) from exc
        except httpx.TransportError as exc:
            logger.warning("embervm session listing transport error: %s", exc)
            raise EmberVMTransportError(str(exc)) from exc

    async def destroy_session(self, ember_session_id: str) -> dict:
        """Destroy one control plane session by its EmberVM session id (management auth).

        Args:
            ember_session_id: The control plane session id (s-...) to destroy.

        Returns:
            The control plane's destroy response.

        Raises:
            EmberVMTransportError: If the destroy request fails, including a
                404 for a session that is already gone.
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        url = f"{EMBERVM_URL}/v1/sessions/{ember_session_id}"
        headers = auth_headers()
        timeout = httpx.Timeout(self.read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.delete(url, headers=headers)
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException as exc:
            logger.warning(
                "embervm session destroy timed out for session %s: %s",
                ember_session_id,
                exc,
            )
            raise EmberVMTimeout(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "embervm session destroy failed for session %s: %s",
                ember_session_id,
                exc,
            )
            raise EmberVMTransportError(_status_error_detail(exc)) from exc
        except httpx.TransportError as exc:
            logger.warning(
                "embervm session destroy transport error for session %s: %s",
                ember_session_id,
                exc,
            )
            raise EmberVMTransportError(str(exc)) from exc

    async def deliver(
        self,
        ember: EmberSession | None,
        cli_session_id: str | None,
        message: str,
        model: str | None = None,
        restore_from: str | None = None,
        on_create: Callable[[EmberSession], Awaitable[None]] | None = None,
    ) -> tuple[Turn, EmberSession]:
        """Execute one turn on the guest session and return the result.

        Args:
            ember: Existing EmberVM session identity to reuse, or None to create new.
            cli_session_id: Claude CLI session ID for transcript resumption.
                When ember is None and restore_from is set, this should be
                the PRIOR generation's cli_session_id (#4306 slice 5): it is
                only actually sent once the restore is confirmed to have
                recovered the workspace (see restore_from below).
            message: User message / prompt to send to Claude.
            restore_from: A prior LINEAGE handle to inherit the guest
                workspace from when ember is None (#4306 slice 5: the
                binding-was-cleared path, e.g. after an EmberSessionGone or
                an admin destroy, where the active binding is gone but the
                durable workspace may still exist). Ignored when ember is
                not None (a live binding is reused as today).

        Returns:
            The guest Turn and the EmberVM session identity actually used.

        Raises:
            EmberVMTransportError: If the invoke fails.
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        timeout = httpx.Timeout(self.read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)

        created = ember is None
        workspace_recovery = None
        if ember is None:
            if restore_from:
                try:
                    ember = await self.create_session(restore_from=restore_from)
                except EmberVMTransportError as restore_exc:
                    # Same degrade as the 410/403 arm below: the restore
                    # create was itself DENIED or timed out, so fall back to
                    # a BLANK session rather than failing the turn outright.
                    logger.warning(
                        "embervm restore create denied for lineage %s, "
                        "falling back to a blank session: %s",
                        restore_from,
                        restore_exc,
                    )
                    ember = await self.create_session()
                    workspace_recovery = {
                        "created": True,
                        "restored": False,
                        "degraded": "restore_denied",
                    }
                else:
                    workspace_recovery = {
                        "created": True,
                        "restored": bool(ember.restored),
                        "degraded": None,
                    }
                if on_create is not None:
                    await on_create(ember)
                # Only resume the CLI transcript when the workspace was
                # ACTUALLY recovered; a blank session has nothing for
                # --resume to find (mirrors the 410 arm's cli gating below).
                cli_session_id = cli_session_id if ember.restored else None
            else:
                ember = await self.create_session()
                workspace_recovery = {
                    "created": True,
                    "restored": False,
                    "degraded": None,
                }
                if on_create is not None:
                    await on_create(ember)

        async def invoke(
            current: EmberSession, current_cli_session_id: str | None
        ) -> Turn:
            payload = {"message": message, "session_id": current_cli_session_id}
            if model is not None:
                payload["model"] = model
            body = json.dumps(payload)
            url = f"{EMBERVM_URL}/v1/sessions/{current.session_id}/invoke"
            headers = {
                "Authorization": f"Bearer {current.session_token}",
                "X-Ember-Guest-Path": "/shim/turn",
            }
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        url, content=body.encode(), headers=headers
                    )
                    response.raise_for_status()
                    guest_data = response.json()
                    return Turn(
                        result=guest_data.get("result", ""),
                        terminal_reason=guest_data.get("terminal_reason"),
                        stop_reason=guest_data.get("stop_reason"),
                        is_error=bool(guest_data.get("is_error", False)),
                        permission_denials=guest_data.get("permission_denials", []),
                        num_turns=int(guest_data.get("num_turns", 0)),
                        session_id=guest_data.get("session_id")
                        or current_cli_session_id,
                        usage=guest_data.get("usage", {}),
                        total_cost_usd=guest_data.get("total_cost_usd"),
                        duration_ms=guest_data.get("duration_ms"),
                        activities=guest_data.get("activities", []),
                    )
            except httpx.TimeoutException as exc:
                logger.warning(
                    "embervm invoke timed out for session %s: %s",
                    current.session_id,
                    exc,
                )
                raise EmberVMTimeout(str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "embervm invoke failed for session %s: %s",
                    current.session_id,
                    exc,
                )
                raise
            except httpx.TransportError as exc:
                logger.warning(
                    "embervm invoke transport error for session %s: %s",
                    current.session_id,
                    exc,
                )
                raise EmberVMTransportError(str(exc)) from exc

        try:
            turn = await _invoke_with_retryable_backoff(
                lambda: invoke(ember, cli_session_id)
            )
            if workspace_recovery is not None:
                turn = turn._replace(workspace_recovery=workspace_recovery)
            return turn, ember
        except httpx.HTTPStatusError as exc:
            if created or exc.response.status_code not in (403, 410):
                raise EmberVMTransportError(_status_error_detail(exc)) from exc

            # #4306 slice 4: restore keys on the LINEAGE handle, not
            # session_id (session_id == lineage_id only for a gen-0 create;
            # a session_id from a PRIOR restore is not a valid restore key,
            # restoring it 404s as unknown_lineage). Falls back to
            # session_id for a pre-lineage binding: a row persisted before
            # slice 3 landed never got a lineage_id back, so ember.lineage_id
            # is None and session_id is the only handle it ever had.
            restore_from = ember.lineage_id or ember.session_id
            try:
                new_ember = await self.create_session(restore_from=restore_from)
            except EmberVMTransportError as restore_exc:
                # The restore create was itself DENIED (unknown_lineage, a
                # workload/principal mismatch, a live heir, or a concurrent
                # restore of the same lineage already in flight) or timed
                # out: the workspace is not recoverable right now. Degrade
                # to a BLANK session rather than bricking the turn; losing
                # the transcript/workspace is better than failing outright.
                logger.warning(
                    "embervm restore create denied for lineage %s, "
                    "falling back to a blank session: %s",
                    restore_from,
                    restore_exc,
                )
                new_ember = await self.create_session()

            if on_create is not None:
                await on_create(new_ember)
            workspace_recovery = {
                "created": True,
                "restored": bool(new_ember.restored),
                "degraded": None if new_ember.restored else "restore_fallback",
            }

            # Only resume the CLI transcript when the guest workspace was
            # ACTUALLY recovered; a blank workspace has nothing for
            # --resume to find.
            cli = cli_session_id if new_ember.restored else None
            try:
                turn = await _invoke_with_retryable_backoff(
                    lambda: invoke(new_ember, cli)
                )
                return turn._replace(workspace_recovery=workspace_recovery), new_ember
            except (httpx.HTTPStatusError, EmberVMTransportError) as retry_exc:
                if isinstance(retry_exc, httpx.HTTPStatusError):
                    raise EmberSessionGone(
                        _status_error_detail(retry_exc)
                    ) from retry_exc
                raise EmberSessionGone(str(retry_exc)) from retry_exc
