"""EmberVM shim transport for Claude Code session execution.

The Claude Code CLI runs in an EmberVM guest, not in the monolith pod.
The guest exposes a session API through the EmberVM control plane.

Reuses the patterns from faas/embervm_client.py (httpx.AsyncClient,
EMBERVM_URL, auth headers, error types, timeout shape).
"""

from __future__ import annotations

import json
import logging
import os
from typing import NamedTuple, Protocol

import httpx

from faas.embervm_client import (
    EmberVMTimeout,
    EmberVMTransportError,
    SUBMIT_CONNECT_TIMEOUT,
)
from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)


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


class EmberSession(NamedTuple):
    session_id: str
    session_token: str
    expires_at: int | None


class ShimTransport(Protocol):
    async def deliver(
        self,
        ember: EmberSession | None,
        cli_session_id: str | None,
        message: str,
        model: str | None = None,
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

    async def create_session(self) -> EmberSession:
        """Create a new session on the guest and return the session ID.

        Returns:
            The EmberVM session identity and bearer token.

        Raises:
            EmberVMTransportError: If session creation fails.
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        url = f"{EMBERVM_URL}/v1/workloads/{self.workload}/sessions"
        headers = auth_headers()
        timeout = httpx.Timeout(self.read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers)
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
                )
        except httpx.TimeoutException as exc:
            logger.warning("embervm session creation timed out: %s", exc)
            raise EmberVMTimeout(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("embervm session creation failed: %s", exc)
            raise EmberVMTransportError(_status_error_detail(exc)) from exc
        except httpx.TransportError as exc:
            logger.warning("embervm session creation transport error: %s", exc)
            raise EmberVMTransportError(str(exc)) from exc

    async def deliver(
        self,
        ember: EmberSession | None,
        cli_session_id: str | None,
        message: str,
        model: str | None = None,
    ) -> tuple[Turn, EmberSession]:
        """Execute one turn on the guest session and return the result.

        Args:
            ember: Existing EmberVM session identity to reuse, or None to create new.
            cli_session_id: Claude CLI session ID for transcript resumption.
            message: User message / prompt to send to Claude.

        Returns:
            The guest Turn and the EmberVM session identity actually used.

        Raises:
            EmberVMTransportError: If the invoke fails.
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        timeout = httpx.Timeout(self.read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)

        created = ember is None
        if ember is None:
            ember = await self.create_session()

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
            return await invoke(ember, cli_session_id), ember
        except httpx.HTTPStatusError as exc:
            if created or exc.response.status_code not in (403, 410):
                raise EmberVMTransportError(_status_error_detail(exc)) from exc
            ember = await self.create_session()
            try:
                return await invoke(ember, None), ember
            except (httpx.HTTPStatusError, EmberVMTransportError) as retry_exc:
                if isinstance(retry_exc, httpx.HTTPStatusError):
                    raise EmberSessionGone(
                        _status_error_detail(retry_exc)
                    ) from retry_exc
                raise EmberSessionGone(str(retry_exc)) from retry_exc
