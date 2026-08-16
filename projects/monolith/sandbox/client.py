"""Broker for the per-language EmberVM sandbox workloads.

Shared by the MCP tool (sandbox/mcp.py) and the Discord concierge tool
(chat/agent.py). POSTs code to EmberVM; the guest is zero-egress and one-shot,
and this client returns the guest response verbatim.
"""

from __future__ import annotations

import hashlib
import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

EMBERVM_URL = os.environ.get("EMBERVM_URL", "")

SUPPORTED_LANGUAGES = ("python", "go", "rust", "elixir", "ocaml", "javascript")
SANDBOX_WORKLOAD_PREFIX = os.environ.get("SANDBOX_WORKLOAD_PREFIX", "sandbox-")

# EmberVM R4 scratch-postgres DSN (D-R4.PR-11.1). When set (the chart wires it
# from the monolith-namespace 1Password secret + the embervm serving Service),
# agent Python snippets can psycopg-connect to the scale-to-zero scratch
# Postgres. The guest is zero-egress to the public internet but CAN reach the
# in-cluster serving Service, so the DSN is injected into the executed code's
# process env (as os.environ["SCRATCH_POSTGRES_DSN"]) rather than baked into the
# guest image. Empty leaves the guest env untouched (feature off).
SCRATCH_POSTGRES_DSN = os.environ.get("SCRATCH_POSTGRES_DSN", "")

SANDBOX_CONNECT_TIMEOUT = 5.0
# Guest wall-clock cap is 25s inside a 30s workload requestTimeout; read a
# little past that so the daemon's timeout error reaches us intact.
SANDBOX_READ_TIMEOUT = 35.0


def _with_scratch_env(code: str, language: str = "python") -> str:
    """Prepend scratch-tier env setup only to submitted Python code.

    The guest exec protocol carries only code + files (no per-invoke env), so any
    scratch-tier credential reaches the snippet as a tiny preamble that sets the
    guest process env before the user code runs:

    - SCRATCH_POSTGRES_DSN (R4): a snippet can ``os.environ["SCRATCH_POSTGRES_DSN"]``
      and psycopg-connect to the scale-to-zero scratch Postgres.
    Each value is a Python-repr literal so quotes/backslashes/newlines in the secret
    are escaped safely. When the feature is off the code is returned unchanged.
    """
    if language != "python" or not SCRATCH_POSTGRES_DSN:
        return code
    lines = ["import os as _os"]
    if SCRATCH_POSTGRES_DSN:
        lines.append(f"_os.environ['SCRATCH_POSTGRES_DSN'] = {SCRATCH_POSTGRES_DSN!r}")
    lines.append("del _os")
    preamble = "\n".join(lines) + "\n"
    return preamble + code


async def run_code_in_sandbox(
    code: str, language: str = "python", files: list[dict] | None = None
) -> dict:
    """POST code (and optional input files) to a language sandbox workload.

    Returns the daemon's structured response on success: ``stdout``,
    ``stderr``, ``exit_code``, ``files`` (base64-encoded), ``duration_ms``,
    and ``truncated``. On failure, a dict with a single ``error`` key.

    The language selects the workload. Each call is one-shot and has no state
    shared with any other call.
    """
    if language not in SUPPORTED_LANGUAGES:
        valid = ", ".join(SUPPORTED_LANGUAGES)
        return {"error": f"unsupported language {language!r}; valid languages: {valid}"}
    if not code or not code.strip():
        return {"error": "no code provided"}

    payload: dict = {"code": _with_scratch_env(code, language)}
    if files:
        payload["files"] = files

    return await _run_embervm(payload, language)


async def _run_embervm(payload: dict, language: str) -> dict:
    """POST a run to its language workload and return the guest response.

    Submits synchronously (``?wait=true``). EmberVM forwards the guest response
    verbatim, so the shape matches the sandbox contract. Idempotency-Key from
    the language and code hash dedupes retries.
    """
    if not EMBERVM_URL:
        return {"error": "EMBERVM_URL is not configured"}

    key_material = f"{language}\0{payload.get('code', '')}".encode()
    key = hashlib.sha256(key_material).hexdigest()
    headers = {**auth_headers(), "Idempotency-Key": key}
    timeout = httpx.Timeout(SANDBOX_READ_TIMEOUT, connect=SANDBOX_CONNECT_TIMEOUT)
    workload = f"{SANDBOX_WORKLOAD_PREFIX}{language}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{EMBERVM_URL}/v1/workloads/{workload}/tasks?wait=true",
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
