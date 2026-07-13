"""Cloudflare Turnstile siteverify for public-chat admission (ADR 005 layer 1).

This is the admission challenge: no solved Turnstile token, no session. The
siteverify call runs in THIS FastAPI binary (the Turnstile *secret* is a
backend-only OnePasswordItem, never in SSR); SSR forwards only the user's token
and the real client IP. The site key is public by design; the secret key is a
verification-only credential.

The call is the FIRST off-cluster egress from the public namespace. This
destination (challenges.cloudflare.com:443) is the single sanctioned off-cluster
FQDN in the public tier's CiliumNetworkPolicy egress rules (ADR platform/012's
translation of the old default-deny egress); see
projects/monolith-public/chart/templates/cilium-policy.yaml. Nothing else opens.

Fail-closed posture: any verification we cannot complete (network error, timeout,
non-2xx, malformed body) is treated as a failure, never a pass.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Cloudflare's siteverify endpoint (the one sanctioned off-cluster egress).
SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Verification-only secret, backend-only. Unset in dev/test (see siteverify);
# production always injects it from the cloudflare-turnstile OnePasswordItem.
SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")

# Per-attempt timeout. The connect leg is the flaky one (establishing the
# off-cluster TLS connection to Cloudflare), so it gets a more generous budget
# than the read.
_TIMEOUT = httpx.Timeout(
    float(os.environ.get("TURNSTILE_TIMEOUT_SECONDS", "6")),
    connect=float(os.environ.get("TURNSTILE_CONNECT_TIMEOUT_SECONDS", "8")),
)

# Retry transient connect/network failures: the off-cluster egress to Cloudflare
# is occasionally slow to establish and a single attempt fails closed with a
# ConnectError, which blocks admission. A ConnectError means the request never
# reached Cloudflare, so the token was NOT consumed and retrying is safe.
_VERIFY_ATTEMPTS = int(os.environ.get("TURNSTILE_VERIFY_ATTEMPTS", "3"))
_RETRYABLE_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
)


@dataclass(frozen=True)
class TurnstileResult:
    """The outcome of a siteverify call.

    ``success`` is the only admission decision. ``outcome`` is the stable string
    persisted to ``sessions.turnstile_outcome`` (passed / failed / stub_accept).
    The remaining fields are the diagnostic correlates Cloudflare returns; they
    are not used as a security boundary, only for logging/forensics.
    """

    success: bool
    outcome: str
    error_codes: tuple[str, ...] = ()
    action: str | None = None
    hostname: str | None = None


async def siteverify(token: str | None, remoteip: str | None = None) -> TurnstileResult:
    """Verify a Turnstile token against Cloudflare's siteverify endpoint.

    POSTs the form fields ``secret`` (env), ``response`` (the user token), and
    ``remoteip`` (the forwarded client IP, when present). Returns a small result;
    callers admit only when ``success`` is True.

    Dev/test fallback: if ``TURNSTILE_SECRET_KEY`` is unset we stub-accept so
    local dev and unit tests work without a live challenge. This branch is
    clearly marked and never taken in production, where the secret is always set.
    """
    if not SECRET_KEY:
        # Stubbed-accept fallback (dev/test only). Never reached in prod.
        logger.warning(
            "chat_public.turnstile.stub_accept TURNSTILE_SECRET_KEY unset; "
            "accepting without a live challenge (dev/test only)"
        )
        return TurnstileResult(success=True, outcome="stub_accept")

    if not token:
        # No token to verify is a failed challenge, not an error.
        return TurnstileResult(
            success=False,
            outcome="failed",
            error_codes=("missing-input-response",),
        )

    data = {"secret": SECRET_KEY, "response": token}
    if remoteip:
        data["remoteip"] = remoteip

    body = None
    for attempt in range(_VERIFY_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(SITEVERIFY_URL, data=data)
                resp.raise_for_status()
                body = resp.json()
            break
        except _RETRYABLE_ERRORS as exc:
            # Transient egress failure; the request did not reach Cloudflare, so
            # the token is unspent and we can retry safely.
            if attempt + 1 < _VERIFY_ATTEMPTS:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            logger.warning(
                "chat_public.turnstile.verify_error err=%s attempts=%d",
                type(exc).__name__,
                attempt + 1,
            )
            return TurnstileResult(
                success=False,
                outcome="failed",
                error_codes=("verify-unreachable",),
            )
        except (httpx.HTTPError, ValueError) as exc:
            # Non-retryable (a real 4xx/5xx from Cloudflare or a malformed body):
            # fail closed without retrying.
            logger.warning(
                "chat_public.turnstile.verify_error err=%s", type(exc).__name__
            )
            return TurnstileResult(
                success=False,
                outcome="failed",
                error_codes=("verify-unreachable",),
            )

    success = bool(body.get("success"))
    return TurnstileResult(
        success=success,
        outcome="passed" if success else "failed",
        error_codes=tuple(body.get("error-codes") or ()),
        action=body.get("action"),
        hostname=body.get("hostname"),
    )
