"""Thin async submit client for EmberVM sync task submission (FaaS smoke + router).

The FaaS ingestion smoke run (Task 10) and, later, the invocation router (Task
11) both marshal an HTTP request into an EmberVM sync submit and read the
guest's response back verbatim. This module owns that single call so the two
callers cannot drift.

Mirrors sandbox/client.py's ``_run_embervm`` shape (httpx.AsyncClient, EMBERVM_URL,
auth_headers), with two differences: the body is forwarded to the guest VERBATIM
(``content=`` raw bytes, never ``json=``), and we return the raw ``httpx.Response``
so the caller reads status/headers/body itself.

CRITICAL: ``guest_path`` MUST be the workload's ``invokePath`` (default
``/invoke``). EmberVM's default guest path is ``/``; a workload whose invokePath
is ``/invoke`` 404s if the submit does not carry ``X-Ember-Guest-Path``
(documented Task 14a gotcha).
"""

from __future__ import annotations

import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

EMBERVM_URL = os.environ.get("EMBERVM_URL", "")

# Connect fast; the read timeout is caller-supplied (a smoke run reads a little
# past the 30s workload requestTimeout so the daemon's own timeout reaches us).
SUBMIT_CONNECT_TIMEOUT = 5.0


class EmberVMTransportError(RuntimeError):
    """Raised when the submit could not reach EmberVM or timed out.

    Distinct from a guest response with a non-2xx status: a transport error is
    the only smoke failure the caller may retry (import errors surface as a
    guest 5xx and never retry, per the plan's open-risks row).
    """


async def submit(
    name: str,
    *,
    body: bytes,
    guest_path: str,
    extra_guest_headers: dict[str, str] | None = None,
    read_timeout: float,
) -> httpx.Response:
    """POST ``body`` to workload ``name``'s sync submit, returning the raw Response.

    Forwards ``body`` to the guest verbatim (``content=``). ``guest_path`` is set
    as ``X-Ember-Guest-Path`` (the workload's invokePath), and each entry of
    ``extra_guest_headers`` is forwarded as ``X-Ember-Guest-<k>: <v>``.

    Raises :class:`EmberVMTransportError` on connect failure or timeout (the
    retryable class); any 2xx/4xx/5xx guest response is returned, not raised.
    """
    if not EMBERVM_URL:
        raise EmberVMTransportError("EMBERVM_URL is not configured")

    headers = {**auth_headers(), "X-Ember-Guest-Path": guest_path}
    for key, value in (extra_guest_headers or {}).items():
        headers[f"X-Ember-Guest-{key}"] = value

    timeout = httpx.Timeout(read_timeout, connect=SUBMIT_CONNECT_TIMEOUT)
    url = f"{EMBERVM_URL}/v1/workloads/{name}/tasks?wait=true"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, content=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("embervm submit transport error for %s: %s", name, exc)
        raise EmberVMTransportError(str(exc)) from exc
