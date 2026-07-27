"""Broker for the fc-invoke sandbox workload (ADR agents/044).

Shared by the MCP tool (sandbox/mcp.py) and the Discord concierge tool
(chat/agent.py). POSTs code to the in-cluster fc-invoke daemon; the guest is
zero-egress and one-shot, so this client is the only stateful party.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

# EmberVM control-plane base URL + dispatch (R0 cutover). EMBERVM_URL is shared
# with the semgrep client (wired by the chart). SANDBOX_DISPATCH selects where a
# python run is served: fc-invoke (default) or embervm (EmberVM's sandbox
# Workload). Same guest contract, so the response shape is identical.
EMBERVM_URL = os.environ.get("EMBERVM_URL", "")
SANDBOX_DISPATCH = os.environ.get("SANDBOX_DISPATCH", "fc-invoke")

# The EmberVM session-class workload backing sessioned run_python (R2, plan
# Task 10). Sessions are created under this workload; the one-shot task class
# stays on `sandbox`. Injected from Helm values so the name is never hardcoded
# to a specific release wiring.
SANDBOX_SESSION_WORKLOAD = os.environ.get("SANDBOX_SESSION_WORKLOAD", "sandbox-session")

# EmberVM R4 scratch-postgres DSN (D-R4.PR-11.1). When set (the chart wires it
# from the monolith-namespace 1Password secret + the embervm serving Service),
# agent run_python snippets can psycopg-connect to the scale-to-zero scratch
# Postgres. The guest is zero-egress to the public internet but CAN reach the
# in-cluster serving Service, so the DSN is injected into the executed code's
# process env (as os.environ["SCRATCH_POSTGRES_DSN"]) rather than baked into the
# guest image. Empty leaves the guest env untouched (feature off).
SCRATCH_POSTGRES_DSN = os.environ.get("SCRATCH_POSTGRES_DSN", "")

SANDBOX_CONNECT_TIMEOUT = 5.0
# Guest wall-clock cap is 25s inside a 30s workload requestTimeout; read a
# little past that so the daemon's timeout error reaches us intact.
SANDBOX_READ_TIMEOUT = 35.0
# A relight of a banked session adds restore latency before the guest runs, so
# a session invoke reads a little longer than a one-shot to let a cold-banked
# session relight and still return within the read window.
SANDBOX_SESSION_READ_TIMEOUT = 45.0


def _with_scratch_env(code: str) -> str:
    """Prepend scratch-tier env setup to the submitted code when features are on.

    The guest exec protocol carries only code + files (no per-invoke env), so any
    scratch-tier credential reaches the snippet as a tiny preamble that sets the
    guest process env before the user code runs. Two independent features:

    - SCRATCH_POSTGRES_DSN (R4): a snippet can ``os.environ["SCRATCH_POSTGRES_DSN"]``
      and psycopg-connect to the scale-to-zero scratch Postgres.
    Each value is a Python-repr literal so quotes/backslashes/newlines in the secret
    are escaped safely. When the feature is off the code is returned unchanged.
    """
    if not SCRATCH_POSTGRES_DSN:
        return code
    lines = ["import os as _os"]
    if SCRATCH_POSTGRES_DSN:
        lines.append(f"_os.environ['SCRATCH_POSTGRES_DSN'] = {SCRATCH_POSTGRES_DSN!r}")
    lines.append("del _os")
    preamble = "\n".join(lines) + "\n"
    return preamble + code


async def run_python_in_sandbox(
    code: str, files: list[dict] | None = None, session: str | None = None
) -> dict:
    """POST code (and optional input files) to the sandbox workload.

    Returns the daemon's structured response on success: ``stdout``,
    ``stderr``, ``exit_code``, ``files`` (base64-encoded), ``duration_ms``,
    and ``truncated``. On failure, a dict with a single ``error`` key.

    When ``session`` is a non-empty handle, the run is served by the EmberVM
    session class (R2): state persists best-effort across calls that share the
    handle. The first use of a handle creates a session; later uses reuse it; a
    session that has been reset (expired, evicted, or failed) is transparently
    re-created and the response carries ``session_reset: True`` so the caller
    knows prior state was lost. When ``session`` is absent the behavior is the
    one-shot task class, exactly as before (no state, no session table touch).
    """
    if not code or not code.strip():
        return {"error": "no code provided"}

    payload: dict = {"code": _with_scratch_env(code)}
    if files:
        payload["files"] = files

    if session:
        return await _run_session(session, payload)

    if not FC_INVOKE_URL:
        return {"error": "FC_INVOKE_URL is not configured"}
    if SANDBOX_DISPATCH == "embervm":
        return await _run_embervm(payload)
    return await _run_fc_invoke(payload)


async def _run_fc_invoke(payload: dict) -> dict:
    """POST to the fc-invoke sandbox workload (the default path)."""
    if not FC_INVOKE_URL:
        return {"error": "FC_INVOKE_URL is not configured"}

    timeout = httpx.Timeout(SANDBOX_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Carry this pod's ServiceAccount token so fc-invoke's TokenReview
            # gate admits the call; off-cluster this is an empty header set.
            resp = await client.post(
                f"{FC_INVOKE_URL}/invoke/sandbox",
                json=payload,
                headers=auth_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        logger.exception("fc-invoke connection failed")
        return {"error": f"could not reach fc-invoke: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("fc-invoke returned an error status")
        return {
            "error": (
                f"fc-invoke returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001: surface any failure as structured error
        logger.exception("sandbox execution failed")
        return {"error": f"sandbox execution failed: {exc}"}


async def _run_embervm(payload: dict) -> dict:
    """POST a python run to EmberVM's ``sandbox`` Workload (the R0 cutover). Submits
    synchronously (``?wait=true``); EmberVM forwards the guest response verbatim, so
    the shape matches fc-invoke. Idempotency-Key from the code hash dedupes retries.
    """
    if not EMBERVM_URL:
        return {"error": "EMBERVM_URL is not configured"}

    key = hashlib.sha256(payload.get("code", "").encode()).hexdigest()
    headers = {**auth_headers(), "Idempotency-Key": key}
    timeout = httpx.Timeout(SANDBOX_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{EMBERVM_URL}/v1/workloads/sandbox/tasks?wait=true",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        logger.exception("embervm connection failed")
        return {"error": f"could not reach embervm: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("embervm returned an error status")
        return {
            "error": (
                f"embervm returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001: surface any failure as structured error
        logger.exception("sandbox execution failed")
        return {"error": f"sandbox execution failed: {exc}"}


# ---------------------------------------------------------------------------
# Sessioned run_python (EmberVM R2, ADR embervm/001; plan Task 10).
#
# A session is a long-lived logical sandbox whose in-VM state (variables, files,
# warm imports) persists across invokes. The monolith maps a caller-chosen
# handle to an EmberVM session id + capability token in sandbox.session; the
# token is a SECRET and is NEVER logged here. On a 410 (the session expired,
# was evicted, or failed) the handle is transparently re-created and the caller
# is told with session_reset=True that prior state was lost.
# ---------------------------------------------------------------------------


def _session_headers() -> dict:
    """Management-auth headers (this pod's SA token) for session create/delete."""
    return auth_headers()


def _db_session():
    """Open a short DB session for the handle mapping (private-tier only).

    Imported lazily so importing sandbox.client off-cluster (or in the guest
    contract tests) never pulls SQLModel or the DB engine.
    """
    from app.db import get_engine  # noqa: PLC0415
    from sqlmodel import Session  # noqa: PLC0415

    return Session(get_engine())


async def _create_embervm_session() -> dict:
    """POST a new EmberVM session; return {session_id, token, expires_at} or {error}."""
    if not EMBERVM_URL:
        return {"error": "EMBERVM_URL is not configured"}
    timeout = httpx.Timeout(
        SANDBOX_SESSION_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT
    )
    url = f"{EMBERVM_URL}/v1/workloads/{SANDBOX_SESSION_WORKLOAD}/sessions"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=_session_headers())
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        logger.exception("embervm session create connection failed")
        return {"error": f"could not reach embervm: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("embervm session create returned an error status")
        return {
            "error": (
                f"embervm session create HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001: surface any failure as structured error
        logger.exception("embervm session create failed")
        return {"error": f"session create failed: {exc}"}

    if not data.get("session_id") or not data.get("session_token"):
        # Never log the token; log only that the shape was wrong.
        logger.error("embervm session create response missing id or token")
        return {"error": "session create returned an unexpected response"}
    return {
        "session_id": data["session_id"],
        "token": data["session_token"],
        "expires_at": data.get("expires_at"),
    }


def _parse_expires_at(expires_at):
    """EmberVM returns expires_at as epoch ms; store it as an aware datetime."""
    if not expires_at:
        return None
    try:
        return datetime.fromtimestamp(int(expires_at) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


async def create_session(handle: str) -> dict:
    """Create an EmberVM session and bind it to handle in sandbox.session.

    Returns {"session_id": ...} on success (never the token) or {"error": ...}.
    Last-write-wins: re-creating an existing handle rebinds it to a fresh
    session (the transparent-recreate path relies on this).
    """
    from sandbox.repository import upsert_session_row  # noqa: PLC0415

    created = await _create_embervm_session()
    if created.get("error"):
        return created
    with _db_session() as db:
        upsert_session_row(
            db,
            handle=handle,
            session_id=created["session_id"],
            token=created["token"],
            expires_at=_parse_expires_at(created.get("expires_at")),
        )
    return {"session_id": created["session_id"]}


async def _invoke_embervm_session(session_id: str, token: str, payload: dict) -> dict:
    """POST a session invoke; return the guest exec dict, or {"__status__": 410}.

    A 410 is returned as a sentinel so the caller can transparently re-create
    rather than surfacing the raw error; every other HTTP error is a normal
    structured error dict. The session token is sent as the bearer credential
    and is NEVER logged.
    """
    if not EMBERVM_URL:
        return {"error": "EMBERVM_URL is not configured"}
    body = {**payload, "mode": "session"}
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(
        SANDBOX_SESSION_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT
    )
    url = f"{EMBERVM_URL}/v1/sessions/{session_id}/invoke"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 410:
                return {"__status__": 410}
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        logger.exception("embervm session invoke connection failed")
        return {"error": f"could not reach embervm: {exc}"}
    except httpx.HTTPStatusError as exc:
        logger.exception("embervm session invoke returned an error status")
        return {
            "error": (
                f"embervm session invoke HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            )
        }
    except Exception as exc:  # noqa: BLE001: surface any failure as structured error
        logger.exception("embervm session invoke failed")
        return {"error": f"session invoke failed: {exc}"}


async def invoke_session(
    handle: str, code: str, files: list[dict] | None = None
) -> dict:
    """Run code in the session bound to handle, creating or re-creating as needed.

    Create-on-first-use, reuse thereafter, and transparent re-create on a 410
    (the session expired, was evicted, or failed). When a re-create happens the
    returned dict carries session_reset=True so the model knows the accreted
    state (variables, files, imports) was lost and must be re-established.
    """
    if not code or not code.strip():
        return {"error": "no code provided"}
    payload: dict = {"code": code}
    if files:
        payload["files"] = files
    return await _run_session(handle, payload)


async def _run_session(handle: str, payload: dict) -> dict:
    """Core sessioned dispatch: look up the handle, invoke, recreate on 410."""
    from sandbox.repository import get_session_row  # noqa: PLC0415

    reset = False
    with _db_session() as db:
        row = get_session_row(db, handle)
    if row is None:
        created = await create_session(handle)
        if created.get("error"):
            return created
        reset = True  # a fresh session has empty state
        with _db_session() as db:
            row = get_session_row(db, handle)
        if row is None:
            return {"error": "session row vanished after create"}

    result = await _invoke_embervm_session(row.session_id, row.token, payload)
    if result.get("__status__") == 410:
        # The session is terminal; re-create transparently and invoke once more.
        created = await create_session(handle)
        if created.get("error"):
            return created
        reset = True
        with _db_session() as db:
            fresh = get_session_row(db, handle)
        if fresh is None:
            return {"error": "session row vanished after re-create"}
        result = await _invoke_embervm_session(fresh.session_id, fresh.token, payload)
        if result.get("__status__") == 410:
            # A brand-new session should never 410 immediately; surface it.
            return {"error": "session was terminal immediately after re-create"}

    if result.get("error"):
        return result
    # The guest itself may report a reset (a snippet timeout killed the child);
    # OR-fold it with a control-plane recreate so either cause is reported.
    result["session_reset"] = bool(result.get("session_reset")) or reset
    return result


async def close_session(handle: str) -> dict:
    """Destroy the EmberVM session bound to handle and drop the mapping row.

    Idempotent: an unknown handle or an already-terminal session both return
    {"closed": True}. The DELETE uses management auth (this pod's SA token).
    """
    from sandbox.repository import delete_session_row, get_session_row  # noqa: PLC0415

    with _db_session() as db:
        row = get_session_row(db, handle)
    if row is None:
        return {"closed": True}

    if EMBERVM_URL:
        timeout = httpx.Timeout(SANDBOX_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT)
        url = f"{EMBERVM_URL}/v1/sessions/{row.session_id}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.delete(url, headers=_session_headers())
                # 200 destroyed, 404 already gone: both fine.
                if resp.status_code not in (200, 404):
                    resp.raise_for_status()
        except Exception:  # noqa: BLE001: best-effort destroy; still drop the row
            logger.exception("embervm session destroy failed; dropping mapping anyway")

    with _db_session() as db:
        delete_session_row(db, handle)
    return {"closed": True}
