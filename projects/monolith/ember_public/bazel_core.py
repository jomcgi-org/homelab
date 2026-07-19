"""Bazel skyframe query demo core (ADR embervm/010).

Each visitor query runs against the `bazel-query` EmberVM workload: a
disposable CoW clone restored from a snapshot of a warm Bazel server (the
Abseil analysis graph already resident in the JVM heap). Task-class `Assign`
destroys the clone after the single response, so there is no idle-TTL logic
here, only the submit and its error mapping.

This module must stay importable in the public closure (see
ember_public/__init__.py's docstring): it imports faas.embervm_client, which
is already part of the public-safe surface (no sandbox.client, no demos).

Gating mirrors core.py's demo-postgres helpers:
  - validate_expr: the primary defense-in-depth gate (the guest validates
    again; see runtimes/bazel/guest-init). A whitespace-delimited token
    starting with "-" is rejected outright so a visitor cannot smuggle a
    bazel flag (e.g. --output=starlark, which is code execution) through the
    single `expression` argv element.
  - check_and_record_query: a per-session token bucket, one query per
    _RATE_LIMIT_WINDOW_S seconds, keyed on a salted hash of the session
    cookie rather than the cookie itself.
  - try_acquire_query_slot / release_query_slot: a module-level semaphore
    sized to the workload's `cap` (2), so a burst of visitors cannot pile
    more concurrent tasks onto the workload than it has clones for.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from time import monotonic

from faas import embervm_client
from faas.embervm_client import EmberVMTimeout, EmberVMTransportError

logger = logging.getLogger(__name__)

_WORKLOAD_NAME = "bazel-query"
_GUEST_PATH = "/query"
_READ_TIMEOUT_S = 25.0

_MAX_EXPR_LEN = 512
# Letters, digits, and the punctuation a cquery expression legitimately needs:
# path separators, package/target markers, wildcards, parens for function
# calls, quotes for string args, comma for multi-arg functions, "=" for
# kind()-style attribute matches, space between tokens.
_EXPR_CHARSET_RE = re.compile(r'^[A-Za-z0-9_/:.@~+*\-(),"\'= ]+$')
_TOKEN_RE = re.compile(r"\S+")


def validate_expr(expr: str) -> str | None:
    """Returns an error string if `expr` is not a safe cquery expression,
    else None. This is the primary gate; the guest validates again
    (defense in depth), but nothing unsafe should reach the guest at all."""
    if not expr or not expr.strip():
        return "expression must not be empty"
    if "\n" in expr or "\r" in expr:
        return "expression must be a single line"
    if len(expr) > _MAX_EXPR_LEN:
        return f"expression exceeds {_MAX_EXPR_LEN} characters"
    if not _EXPR_CHARSET_RE.match(expr):
        return "expression contains disallowed characters"
    for token in _TOKEN_RE.findall(expr):
        if token.startswith("-"):
            return "expression must not contain flag-like tokens"
    return None


# ---------------------------------------------------------------------------
# Per-session rate limit: one query per _RATE_LIMIT_WINDOW_S seconds. Keyed on
# a salted hash of the session cookie, not the raw cookie value, so a visitor
# who rotates or grows an oversized cookie cannot grow this dict unboundedly
# (mirrors core.py's demo_pg_session_tag). Bounded further by pruning stale
# entries on access.
# ---------------------------------------------------------------------------

_RATE_LIMIT_WINDOW_S = 3.0
_RATE_LIMIT_PRUNE_AGE_S = 3600.0
_rate_bucket: dict[str, float] = {}


# Optional salt mixed into the rate-limit key so it is not a bare sha256 of
# the cookie value (mirrors chat_public.sessions.IP_HASH_SALT). No default:
# an empty salt is acceptable for dev/test; production injects one via
# BAZEL_QUERY_SESSION_SALT.
_SESSION_SALT = os.environ.get("BAZEL_QUERY_SESSION_SALT", "")


def _session_tag(session_cookie: str) -> str:
    """Hash the session cookie into a short, bounded, opaque rate-limit key
    so a rotated or oversized cookie cannot grow _rate_bucket unboundedly."""
    return hashlib.sha256((_SESSION_SALT + session_cookie).encode()).hexdigest()[:16]


def _prune_rate_bucket(now: float) -> None:
    stale = [
        tag
        for tag, last in _rate_bucket.items()
        if (now - last) > _RATE_LIMIT_PRUNE_AGE_S
    ]
    for tag in stale:
        del _rate_bucket[tag]


def check_and_record_query(session_cookie: str) -> bool:
    """True if this query is allowed; records ONLY an allowed attempt (a
    rejected attempt does not reset the visitor's own window, mirroring
    core.check_and_record_insert)."""
    tag = _session_tag(session_cookie)
    now = monotonic()
    _prune_rate_bucket(now)
    last = _rate_bucket.get(tag)
    if last is not None and (now - last) < _RATE_LIMIT_WINDOW_S:
        return False
    _rate_bucket[tag] = now
    return True


# ---------------------------------------------------------------------------
# Global query semaphore, sized to the workload's `cap` (2 primed/live clones;
# see projects/embervm/chart/values.yaml bazelQueryWorkload.cap). Non-blocking
# acquire: an exhausted semaphore returns an in-band busy response rather than
# queuing (mirrors core.py's demo-postgres semaphore).
# ---------------------------------------------------------------------------

_QUERY_SEMAPHORE_SIZE = 2
_query_semaphore = asyncio.Semaphore(_QUERY_SEMAPHORE_SIZE)


def try_acquire_query_slot() -> bool:
    """Non-blocking acquire. Caller MUST release_query_slot() iff this is True."""
    if _query_semaphore.locked():
        return False
    _query_semaphore._value -= 1
    return True


def release_query_slot() -> None:
    _query_semaphore.release()


# ---------------------------------------------------------------------------
# The submit + response mapping.
# ---------------------------------------------------------------------------

_ANALYZED_LINE_OK_MARKER = "0 packages loaded"


def _check_drift(analyzed_line: str | None) -> None:
    """Log a WARNING when a served response re-analyzed packages: the base
    snapshot's whole point is that Skyframe is already warm, so nonzero
    packages loaded means either the flags drifted from the warming run or
    the snapshot was served cold (ADR condition 2)."""
    if analyzed_line and _ANALYZED_LINE_OK_MARKER not in analyzed_line:
        logger.warning(
            "bazel-query drift: analyzed_line lacks '%s': %r",
            _ANALYZED_LINE_OK_MARKER,
            analyzed_line,
        )


async def run_query(expr: str) -> tuple[int, dict]:
    """Submit `expr` to the bazel-query workload and return (status, payload).

    - transport failure -> 502
    - timeout -> 504
    - guest 422 (bad query) -> passed through, error text from the guest body
    - guest 200 -> forwarded verbatim, with a drift check on analyzed_line
    """
    body = json.dumps({"expression": expr}).encode()
    try:
        resp = await embervm_client.submit(
            _WORKLOAD_NAME,
            body=body,
            guest_path=_GUEST_PATH,
            read_timeout=_READ_TIMEOUT_S,
        )
    except EmberVMTimeout as exc:
        logger.warning("bazel-query submit timed out: %s", exc)
        return 504, {"error": f"query timed out: {exc}"}
    except EmberVMTransportError as exc:
        logger.warning("bazel-query submit transport error: %s", exc)
        return 502, {"error": f"could not reach the query workload: {exc}"}

    if resp.status_code == 200:
        payload = resp.json()
        _check_drift(payload.get("analyzed_line"))
        return 200, payload

    if resp.status_code == 422:
        return 422, {"error": resp.text}

    logger.warning(
        "bazel-query guest returned unexpected status %s: %s",
        resp.status_code,
        resp.text[:500],
    )
    return resp.status_code, {"error": resp.text}
