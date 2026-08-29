"""EmberVM shim transport for Claude Code session execution.

The Claude Code CLI runs in an EmberVM guest, not in the monolith pod.
The guest exposes a session API through the EmberVM control plane.

Reuses the patterns from faas/embervm_client.py (httpx.AsyncClient,
EMBERVM_URL, auth headers, error types, timeout shape).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import random
import re
import zlib
from typing import Awaitable, Callable, NamedTuple, Protocol

import httpx

import agent_sessions
from agent_sessions import model_family
from faas.embervm_client import (
    EmberVMTimeout,
    EmberVMTransportError,
    SUBMIT_CONNECT_TIMEOUT,
)
from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

# Session listing is an in-memory control-plane read serving the console's
# VM-state poll; it must never inherit the turn-sized read timeout.
LIST_SESSIONS_READ_TIMEOUT = 5.0

# Destroy runs on the interactive cancel path, once per session, serially. It
# must never inherit the turn-sized read timeout either: three sessions behind
# a wedged control plane would otherwise hold one request for 90 minutes.
DESTROY_SESSION_READ_TIMEOUT = 30.0


def _retryable_from_response(exc: httpx.HTTPStatusError) -> bool:
    try:
        body = exc.response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return error.get("retryable") is True
        return isinstance(body, dict) and body.get("retryable") is True
    except Exception:
        return False


# Reasons EmberVM returns on a 429 that mean "no slot right now", as opposed
# to a transient control-plane problem. See create_denial/2 in the control
# plane router: capacity is 429 with retryable true, and the reason string is
# machine readable on purpose.
_CAPACITY_DENIAL_REASONS = frozenset(
    {"session_cap", "workload_cap", "quota", "no_capacity"}
)

# A capacity denial clears only when a live turn ends, and a pi turn runs until
# the EmberVM invoke watchdog at 900s. The generic retryable ladder below tops
# out around 2 minutes and the create path used to get only 17 seconds, so a
# create that collided with a running turn could never wait long enough and
# failed essentially every time. piRuntimeWorkload.concurrency.cap is 2 and is
# pinned to the KV budget (each session takes 120K of a 262144-token pool), so
# the cap cannot simply be raised: waiting is the only way to not fail.
#
# Sums to roughly 19 minutes, which comfortably outlasts one watchdog-bounded
# turn. A successful attempt returns immediately, so a fast-clearing collision
# is not made slower by the long tail; it only converts creates that would
# have failed into creates that wait.
_CAPACITY_BACKOFF_SECONDS = (5, 10, 20, 30, 45, 60, 60, 90, 90, 120, 120, 150, 150, 180)

# Jitter so several waiters blocked on the same freed slot do not all wake and
# retry in the same instant, which would hand the slot to whoever wins the race
# and send the rest back around the ladder.
_CAPACITY_JITTER = 0.25

# Every OTHER retryable create failure. Not a capacity denial, so it does not
# have to outlast a whole turn, but 17 seconds was far too short: the control
# plane returns 500 prime_failed with retryable true when the gRPC connection
# to noded drops mid-prime, and on 2026-08-29 seven consecutive creates died
# that way at 17 to 22 seconds each while EmberVM itself was healthy (every pod
# Running, zero restarts). The condition cleared on its own and the next create
# succeeded after 3m30s, so the server was right that it was retryable and the
# client simply gave up first.
#
# Sums to about 7 minutes, which covers that observed 3m30s recovery with real
# margin while still failing fast enough that a genuinely stuck control plane
# does not hold a caller for the capacity ladder's 19 minutes. A first draft
# summed to 172s and its own test caught that as short of the 210s it was
# sized for.
_CREATE_RETRY_SECONDS = (2, 5, 10, 20, 30, 45, 60, 90, 90, 90)


def _capacity_denial(exc: httpx.HTTPStatusError) -> bool:
    """True when a 429 says the workload is at capacity, not merely busy."""
    if exc.response.status_code != 429:
        return False
    try:
        body = exc.response.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    return body.get("reason") in _CAPACITY_DENIAL_REASONS


def _capacity_sleep_seconds(attempt: int) -> float:
    """Jittered delay for capacity attempt ``attempt`` (0-based)."""
    base = _CAPACITY_BACKOFF_SECONDS[min(attempt, len(_CAPACITY_BACKOFF_SECONDS) - 1)]
    return base * (1.0 + random.uniform(-_CAPACITY_JITTER, _CAPACITY_JITTER))


async def _invoke_with_retryable_backoff(
    invoke_fn: Callable[[], Awaitable["Turn"]],
    max_attempts: int = 8,
) -> "Turn":
    """Call invoke_fn with exponential backoff on retryable errors.

    The window is ~2 minutes, not seconds, because the dominant retryable
    cause is memory pressure at prime/relight and that clears on VM idle
    TTLs measured in minutes, not on the reclaim-lag race alone. A longer
    tail cannot make a fast-clearing case slower (a successful attempt
    returns immediately); it only converts turns that would have died into
    turns that wait. The per-attempt read timeout is 1800s, so the added
    sleeps stay far inside the caller's budget.
    """
    backoff_seconds = [2, 5, 10, 20, 30, 30, 30]
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


# The pi-family workload override, following the SANDBOX_WORKLOAD_PREFIX
# precedent in the monolith chart: read once at import, from the chart's
# agentSessions.piWorkload value. There is no security semantic here (unlike
# an egress allowlist), so an unset OR blank value means "use the code
# default", not "deny": this is a REVERT LEVER (set it to claude-runtime to
# put qwen back on the old lane by a values edit, no code deploy), never a
# deny-by-default control.
#
# Extracted as a function purely so the env-reading behaviour is testable
# WITHOUT importlib.reload. Reloading this module rebinds EmberSessionGone to a
# fresh class object while other modules (and the test file's own top-level
# import) keep the previous one, so a later `pytest.raises(EmberSessionGone)`
# stops matching. That failure is order dependent and reads like flakiness, so
# do not reintroduce a reload to test this.
def _resolve_pi_workload() -> str:
    """Resolve the pi-family workload name from the environment."""
    return os.environ.get("AGENT_PI_WORKLOAD", "") or agent_sessions.PI_WORKLOAD


PI_WORKLOAD = _resolve_pi_workload()


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
    diff: dict | None = None


def _reject_guest_diff(session_id, reason: str) -> None:
    """Log why a present diff payload was discarded, then discard it.

    Every rejection below returns None, which is also what an absent payload
    returns, so without this the two are indistinguishable: a null diff_blob
    means either the guest sent nothing or the guest sent something this
    rejected, and the database cannot tell you which. A guest that captures
    correctly and fails validation here would look identical to one that never
    captured at all.
    """
    logger.warning("discarding guest diff for session %s: %s", session_id, reason)
    return None


def _guest_diff(value, session_id=None) -> dict | None:
    """Validate optional guest diff metadata without making it turn-critical.

    An absent payload is silent: guests predating diff capture send nothing and
    that is not a fault. Truncated payloads may carry a validated reduced blob.
    A payload that is present but invalid is logged.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return _reject_guest_diff(session_id, "payload is not a mapping")
    if not {"base_sha", "zlib_b64", "truncated"}.issubset(value):
        missing = sorted({"base_sha", "zlib_b64", "truncated"} - set(value))
        return _reject_guest_diff(session_id, f"missing keys {missing}")
    base_sha = value.get("base_sha")
    truncated = value.get("truncated")
    encoded = value.get("zlib_b64")
    if not isinstance(base_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", base_sha
    ):
        return _reject_guest_diff(session_id, "base_sha is not a 40 to 64 char hex sha")
    if not isinstance(truncated, bool):
        return _reject_guest_diff(session_id, "truncated is not a bool")
    if truncated and encoded is None:
        return value
    if not isinstance(encoded, str):
        return _reject_guest_diff(session_id, "zlib_b64 is not a string")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed) > 1024 * 1024:
            return _reject_guest_diff(
                session_id, f"compressed diff is {len(compressed)} bytes, over 1 MiB"
            )
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, 5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            return _reject_guest_diff(
                session_id, f"uncompressed diff exceeds 5 MiB at {len(raw)} bytes"
            )
        if not decompressor.eof:
            return _reject_guest_diff(session_id, "zlib stream is incomplete")
        if decompressor.unused_data:
            return _reject_guest_diff(session_id, "zlib stream has trailing data")
    except (binascii.Error, zlib.error) as exc:
        return _reject_guest_diff(session_id, f"undecodable payload: {exc}")
    return value


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
        on_create: Callable[[EmberSession, str | None], Awaitable[None]] | None = None,
        repo: str | None = None,
        branch: str | None = None,
        progress_token: str | None = None,
        system_prompt: str | None = None,
        reasoning: bool = False,
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
        workload: str = agent_sessions.DEFAULT_WORKLOAD,
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

    def _workload_for(self, model: str | None) -> str:
        """Resolve the target workload for a session of this model.

        model=None resolves to self.workload (the claude family default, see
        model_family), same as every other family that is not "pi".
        """
        if model_family(model) == "pi":
            return PI_WORKLOAD
        return self.workload

    async def create_session(
        self,
        restore_from: str | None = None,
        model: str | None = None,
        _attempt: int = 0,
    ) -> EmberSession:
        """Create a new session on the guest and return the session ID.

        Args:
            restore_from: A prior LINEAGE handle (EmberSession.lineage_id,
                not necessarily session_id past generation zero) to inherit
                the prior generation's guest workspace from (#4306 slice 4:
                cross-generation continuity). None (the default) requests a
                normal, blank create.
            model: The model this session will run, used only to pick the
                target workload (see _workload_for). None resolves to
                self.workload, matching every caller that predates this
                parameter.

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

        url = f"{EMBERVM_URL}/v1/workloads/{self._workload_for(model)}/sessions"
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
            # A capacity denial waits out a whole turn; anything else retryable
            # keeps the original short ladder, because a transient control
            # plane problem clears in seconds and there is no point holding a
            # caller for 19 minutes over one.
            if _capacity_denial(exc):
                if _attempt < len(_CAPACITY_BACKOFF_SECONDS):
                    delay = _capacity_sleep_seconds(_attempt)
                    logger.info(
                        "embervm at capacity, waiting %.0fs for a slot "
                        "(attempt %d of %d)",
                        delay,
                        _attempt + 1,
                        len(_CAPACITY_BACKOFF_SECONDS),
                    )
                    await asyncio.sleep(delay)
                    return await self.create_session(restore_from, model, _attempt + 1)
            elif _attempt < len(_CREATE_RETRY_SECONDS) and _retryable_from_response(
                exc
            ):
                await asyncio.sleep(_CREATE_RETRY_SECONDS[_attempt])
                return await self.create_session(restore_from, model, _attempt + 1)
            raise EmberVMTransportError(_status_error_detail(exc)) from exc
        except httpx.TransportError as exc:
            logger.warning("embervm session creation transport error: %s", exc)
            raise EmberVMTransportError(str(exc)) from exc

    # The operator surface for the workload cap: parked sessions count as live
    # slot holders, so a stale test session denies every new create until it
    # is listed here and destroyed. Both calls use management auth, not a
    # session bearer token, so they act on any session in the workload.

    async def list_sessions(
        self, limit: int = 50, offset: int = 0, workload: str | None = None
    ) -> dict:
        """List the control plane's sessions for one workload (management auth).

        Args:
            limit: Maximum sessions to return (clamped 1-500 server side).
            offset: Offset into the workload's session list.
            workload: Which lane to list; defaults to self.workload (the
                claude runtime). The two lanes are never aggregated: the
                session cap is per workload, so an operator managing a cap
                wants one lane at a time.

        Returns:
            The control plane's paginated session listing.

        Raises:
            EmberVMTransportError: If the listing request fails.
        """
        if not EMBERVM_URL:
            raise EmberVMTransportError("EMBERVM_URL is not configured")

        target_workload = workload if workload is not None else self.workload
        url = f"{EMBERVM_URL}/v1/workloads/{target_workload}/sessions"
        headers = auth_headers()
        # NOT self.read_timeout: that 30-minute value is sized for a turn,
        # and this listing now serves the console's fast VM-state poll. A
        # wedged control plane must fail the poll's degrade path in
        # seconds, not accumulate half-hour requests behind it.
        timeout = httpx.Timeout(
            LIST_SESSIONS_READ_TIMEOUT, connect=SUBMIT_CONNECT_TIMEOUT
        )

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
        # Destroy is on the interactive cancel path, and a wedged control plane
        # that accepts the connection but never answers would otherwise hold the
        # request for the full turn read timeout (1800s) PER session, serially.
        # Same reasoning as LIST_SESSIONS_READ_TIMEOUT above.
        timeout = httpx.Timeout(
            DESTROY_SESSION_READ_TIMEOUT, connect=SUBMIT_CONNECT_TIMEOUT
        )

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
            # Distinguish "already gone" from "failed to destroy" HERE, where the
            # status code is available. Callers that sniff the message string
            # instead can be fooled by a 500 whose URL or body merely contains
            # "404" or "not found", and would then treat a LIVE session as
            # reaped, leaking the capacity slot they meant to reclaim.
            #
            # 403 is deliberately NOT in this set, unlike the invoke path where
            # it does imply a dead session. DELETE is a management-auth route
            # authenticated with the pod ServiceAccount, not the session's own
            # capability token, so its only 403 is "service account not
            # permitted". That fails EVERY destroy at once, and calling it gone
            # would report the whole fleet reaped while every VM stays alive.
            # The control plane's destroy handler answers 200 (destroyed), 202 (destroying), 404 (not found), or 500 (error).
            if exc.response.status_code in (404, 410):
                raise EmberSessionGone(_status_error_detail(exc)) from exc
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
        on_create: Callable[[EmberSession, str | None], Awaitable[None]] | None = None,
        repo: str | None = None,
        branch: str | None = None,
        progress_token: str | None = None,
        system_prompt: str | None = None,
        reasoning: bool = False,
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
            system_prompt: The caller's system prompt, appended to the shim's
                own sandbox prompt. Omitted from the payload if None.
            reasoning: Whether the guest should use high thinking for each invoke.
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
                    ember = await self.create_session(
                        restore_from=restore_from, model=model
                    )
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
                    ember = await self.create_session(model=model)
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
                # Only resume the CLI transcript when the workspace was
                # ACTUALLY recovered; a blank session has nothing for
                # --resume to find (mirrors the 410 arm's cli gating below).
                cli_for_binding = cli_session_id if ember.restored else None
                if on_create is not None:
                    await on_create(ember, cli_for_binding)
                cli_session_id = cli_for_binding
            else:
                ember = await self.create_session(model=model)
                workspace_recovery = {
                    "created": True,
                    "restored": False,
                    "degraded": None,
                }
                if on_create is not None:
                    await on_create(ember, None)

        async def invoke(
            current: EmberSession, current_cli_session_id: str | None
        ) -> Turn:
            payload = {
                "message": message,
                "session_id": current_cli_session_id,
                "thinking": "high" if reasoning else "off",
            }
            if model is not None:
                payload["model"] = model
            if repo is not None:
                payload["repo"] = repo
                payload["branch"] = branch or "main"
            if progress_token is not None:
                payload["progress_token"] = progress_token
            if system_prompt is not None:
                payload["system_prompt"] = system_prompt
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
                        diff=_guest_diff(guest_data.get("diff"), current.session_id),
                    )
            except httpx.TimeoutException as exc:
                logger.warning(
                    "embervm invoke timed out for session %s: %s",
                    current.session_id,
                    exc,
                )
                raise EmberVMTimeout(str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                # _status_error_detail, not str(exc): the guest reports WHY in
                # the response body, and this log is what a human reads first.
                # #4884 was a bare "422 Unprocessable Content" here for every
                # qwen session, while the body said "File Not Found" (llama.cpp
                # rejecting the absolute-form request line the egress proxy
                # forwarded). The body reached the raised error downstream but
                # never the log, so the one layer that saw the cause dropped it.
                logger.warning(
                    "embervm invoke failed for session %s: %s",
                    current.session_id,
                    _status_error_detail(exc),
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
                new_ember = await self.create_session(
                    restore_from=restore_from, model=model
                )
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
                new_ember = await self.create_session(model=model)

            cli_for_binding = cli_session_id if new_ember.restored else None
            if on_create is not None:
                await on_create(new_ember, cli_for_binding)
            workspace_recovery = {
                "created": True,
                "restored": bool(new_ember.restored),
                "degraded": None if new_ember.restored else "restore_fallback",
            }

            # Only resume the CLI transcript when the guest workspace was
            # ACTUALLY recovered; a blank workspace has nothing for
            # --resume to find.
            cli = cli_for_binding
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
