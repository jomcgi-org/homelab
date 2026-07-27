"""EmberVM shim transport for Claude Code session execution.

The Claude Code CLI runs in an EmberVM guest, not in the monolith pod.
The guest exposes a session API through the EmberVM control plane.

Reuses the patterns from faas/embervm_client.py (httpx.AsyncClient,
EMBERVM_URL, auth headers, error types, timeout shape).
"""

from __future__ import annotations

import json
import logging
from typing import NamedTuple, Protocol

import httpx

from faas.embervm_client import (
    EmberVMTimeout,
    EmberVMTransportError,
    SUBMIT_CONNECT_TIMEOUT,
)
from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

# Read from environment; must be set in monolith pod
EMBERVM_URL = ""


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


class ShimTransport(Protocol):
    async def deliver(self, session_id: str | None, message: str) -> Turn: ...


class EmberVmShimTransport:
    """HTTP client transport for the Claude runtime guest over EmberVM control plane.

    The guest runs the Claude Code CLI as a session-based REPL, answering over
    the /shim/turn endpoint. This transport orchestrates session creation and
    invocation via the control plane's /v1/sessions/:id/invoke API.
    """

    def __init__(
        self,
        workload: str = "claude-runtime",
        read_timeout: float = 120.0,
    ) -> None:
        """Initialize transport for a named EmberVM workload.

        Args:
            workload: Name of the EmberVM workload (e.g., 'claude-runtime').
            read_timeout: HTTP read timeout for invoke calls (seconds).
                Should be generous to allow for slow API calls; the guest
                times out independently at 60s for turns, 15s for init.
        """
        self.workload = workload
        self.read_timeout = read_timeout

    async def create_session(self) -> str:
        """Create a new session on the guest and return the session ID.

        Returns:
            The EmberVM session ID (string UUID from the guest).

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
                return data.get("id", "")
        except httpx.TimeoutException as exc:
            logger.warning("embervm session creation timed out: %s", exc)
            raise EmberVMTimeout(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("embervm session creation failed: %s", exc)
            raise EmberVMTransportError(str(exc)) from exc
        except httpx.TransportError as exc:
            logger.warning("embervm session creation transport error: %s", exc)
            raise EmberVMTransportError(str(exc)) from exc

    async def deliver(
        self, session_id: str | None, message: str, workspace: str = "/tmp"
    ) -> Turn:
        """Execute one turn on the guest session and return the result.

        Args:
            session_id: Existing EmberVM session ID to reuse, or None to create new.
            message: User message / prompt to send to Claude.
            workspace: (Ignored; workspace is in the guest, not the pod.)

        Returns:
            A Turn with the Claude CLI's response parsed from the guest.

        Raises:
            EmberVMTransportError: If the invoke fails.
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        # Create a new session if none supplied
        if not session_id:
            session_id = await self.create_session()

        # Prepare the request body for /shim/turn
        body = json.dumps({"message": message, "session_id": session_id})

        # Invoke the guest shim
        url = f"{EMBERVM_URL}/v1/sessions/{session_id}/invoke"
        headers = {
            **auth_headers(),
            "X-Ember-Guest-Path": "/shim/turn",
        }
        timeout = httpx.Timeout(self.read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url, content=body.encode(), headers=headers
                )
                response.raise_for_status()

                # Parse the guest's response into a Turn
                guest_data = response.json()
                return Turn(
                    result=guest_data.get("result", ""),
                    terminal_reason=guest_data.get("terminal_reason"),
                    stop_reason=guest_data.get("stop_reason"),
                    is_error=bool(guest_data.get("is_error", False)),
                    permission_denials=guest_data.get("permission_denials", []),
                    num_turns=int(guest_data.get("num_turns", 0)),
                    session_id=guest_data.get("session_id") or session_id,
                    usage=guest_data.get("usage", {}),
                    total_cost_usd=guest_data.get("total_cost_usd"),
                    duration_ms=guest_data.get("duration_ms"),
                    activities=guest_data.get("activities", []),
                )
        except httpx.TimeoutException as exc:
            logger.warning(
                "embervm invoke timed out for session %s: %s", session_id, exc
            )
            raise EmberVMTimeout(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("embervm invoke failed for session %s: %s", session_id, exc)
            raise EmberVMTransportError(str(exc)) from exc
        except httpx.TransportError as exc:
            logger.warning(
                "embervm invoke transport error for session %s: %s", session_id, exc
            )
            raise EmberVMTransportError(str(exc)) from exc
