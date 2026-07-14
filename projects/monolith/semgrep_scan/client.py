"""Broker for the fc-invoke semgrep workloads (ADR agents/044).

The plain-function half of the semgrep scan path, mirroring
``sandbox/client.py``. It POSTs file contents to the in-cluster ``fc-invoke``
daemon and returns the structured findings, for two workloads sharing the same
wire shape: ``scan_files`` calls ``/invoke/semgrep`` (a per-PR diff scan), and
``scan_files_full`` calls ``/invoke/semgrep-full`` (a whole-repo interfile FULL
scan). The MCP tool (``semgrep_scan/mcp.py``) and the demos router
(``demos/firecracker_api.py``) call ``scan_files``; ``full_scan.py`` calls
``scan_files_full``. Keeping the HTTP logic here means none of them has to
reach through the FastMCP tool wrapper to run a scan.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

# EmberVM control-plane base URL (Task 15). The per-PR diff scan (``scan_files``)
# can be routed to EmberVM's ``semgrep`` Workload instead of fc-invoke, selected by
# SEMGREP_DISPATCH. EmberVM has only the ``semgrep`` diff workload in R0, so the
# whole-repo (``semgrep-full``) and heavy-diff (``semgrep-hi``) paths stay on
# fc-invoke regardless of the flag.
EMBERVM_URL = os.environ.get("EMBERVM_URL", "")

# Dispatch mode for the per-PR semgrep diff scan (ADR + R0 Task 15):
#   fc-invoke : serve from fc-invoke (default, unchanged behaviour).
#   embervm   : serve from EmberVM's semgrep Workload.
#   shadow    : serve from fc-invoke AND mirror to EmberVM asynchronously,
#               comparing finding-count/status and logging divergence with no
#               user-facing effect (the reversible-cutover soak per the plan).
SEMGREP_DISPATCH = os.environ.get("SEMGREP_DISPATCH", "fc-invoke")

# Shadow divergence counters, queryable for the Task 16 acceptance gate. Process-
# local (reset on restart); the per-divergence structured log is the durable
# record in SigNoz.
shadow_stats = {"total": 0, "match": 0, "diverged": 0, "embervm_error": 0}

# Separate connect/read timeouts: a fast connect surfaces a down daemon quickly,
# while a generous read budget (a bit over the daemon ScanTimeout) lets a large
# multi-file scan finish.
SEMGREP_CONNECT_TIMEOUT = 5.0
SEMGREP_READ_TIMEOUT = 90.0

# The whole-repo interfile FULL scan (semgrep-full workload) walks the entire
# repo with cross-file analysis, which is far slower than a per-PR diff scan.
# Same fast connect (a down daemon still fails fast); a much longer read budget
# so the daemon has room to finish before httpx gives up on us. Set a margin
# ABOVE the daemon's 600s requestTimeout so a scan that runs to its budget
# surfaces the daemon's structured error, not an opaque client-side timeout.
SEMGREP_FULL_READ_TIMEOUT = 660.0


async def _post_invoke(workload: str, files: list[dict], read_timeout: float) -> dict:
    """Shared POST to an fc-invoke semgrep workload; used by both scan entrypoints.

    ``workload`` is the ``/invoke/<workload>`` path segment (``semgrep`` for the
    per-PR diff scan, ``semgrep-full`` for the whole-repo interfile scan). Same
    body shape and error handling for both: on success returns the daemon's
    parsed JSON response, on failure a dict with a single ``error`` key.
    """
    if not FC_INVOKE_URL:
        return {"error": "FC_INVOKE_URL is not configured"}
    if not files:
        return {"error": "no files provided to scan"}

    timeout = httpx.Timeout(read_timeout, connect=SEMGREP_CONNECT_TIMEOUT)
    payload = {"files": files}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Carry this pod's ServiceAccount token so fc-invoke's TokenReview
            # gate admits the call; off-cluster this is an empty header set.
            resp = await client.post(
                f"{FC_INVOKE_URL}/invoke/{workload}",
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
        logger.exception("fc-invoke semgrep scan failed")
        return {"error": f"semgrep scan failed: {exc}"}


async def _post_embervm(
    files: list[dict], read_timeout: float, dedupe: bool = True
) -> dict:
    """POST a diff scan to EmberVM's ``semgrep`` Workload; the EmberVM counterpart
    of ``_post_invoke``. Submits synchronously (``?wait=true``) so the guest's
    ScanResult comes back inline (EmberVM forwards the guest response verbatim, so
    the shape matches fc-invoke), with an Idempotency-Key from the content hash so
    a webhook redelivery dedupes to the same task, unless ``dedupe`` is False (the
    demo single-scan path), in which case the header is omitted so a fresh scan
    always runs. Same error shape as ``_post_invoke`` (a dict with a single
    ``error`` key on failure).
    """
    if not EMBERVM_URL:
        return {"error": "EMBERVM_URL is not configured"}
    if not files:
        return {"error": "no files provided to scan"}

    timeout = httpx.Timeout(read_timeout, connect=SEMGREP_CONNECT_TIMEOUT)
    payload = {"files": files}
    headers = auth_headers()
    if dedupe:
        headers["Idempotency-Key"] = _content_key(files)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{EMBERVM_URL}/v1/workloads/semgrep/tasks?wait=true",
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
        logger.exception("embervm semgrep scan failed")
        return {"error": f"embervm semgrep scan failed: {exc}"}


def _content_key(files: list[dict]) -> str:
    """A stable idempotency key from the scan's file contents (path + content),
    order-independent, so a webhook redelivery of the same diff dedupes to the
    same EmberVM task."""
    digest = hashlib.sha256()
    for f in sorted(files, key=lambda e: e.get("path", "")):
        digest.update(f.get("path", "").encode())
        digest.update(b"\0")
        digest.update(f.get("content", "").encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _finding_count(result: dict) -> int:
    findings = result.get("findings")
    return len(findings) if isinstance(findings, list) else -1


async def _shadow_scan(files: list[dict], fc_result: dict) -> None:
    """Fire-and-forget shadow (Task 15): run the same diff on EmberVM, compare
    finding-count and error status to the served fc-invoke result, and log any
    divergence. NEVER raises: a shadow failure must not affect the served scan."""
    try:
        shadow_stats["total"] += 1
        ev_result = await _post_embervm(files, SEMGREP_READ_TIMEOUT)

        if "error" in ev_result:
            shadow_stats["embervm_error"] += 1
            logger.warning(
                "semgrep shadow: embervm path errored: %s", ev_result["error"]
            )
            return

        fc_n, ev_n = _finding_count(fc_result), _finding_count(ev_result)
        if fc_n == ev_n:
            shadow_stats["match"] += 1
        else:
            shadow_stats["diverged"] += 1
            logger.warning(
                "semgrep shadow: finding-count divergence fc=%d embervm=%d", fc_n, ev_n
            )
    except Exception:  # noqa: BLE001: shadow must never affect the served path
        logger.exception("semgrep shadow comparison failed")


async def scan_files(files: list[dict], dedupe: bool = True) -> dict:
    """POST file contents to the semgrep diff workload and return findings.

    Each entry in ``files`` needs a ``path`` (repo-relative, used to pick rules
    and report locations) and a ``content`` (the whole current file text). On
    success returns the daemon response: a ``findings`` list plus an ``errors``
    list. On failure returns a dict with a single ``error`` key.

    Dispatch is selected by ``SEMGREP_DISPATCH`` (Task 15): ``fc-invoke`` (default)
    serves from fc-invoke; ``embervm`` serves from EmberVM's semgrep Workload;
    ``shadow`` serves from fc-invoke and mirrors to EmberVM asynchronously for
    finding-count comparison, with no user-facing effect. The caller contract
    (response/error shape, timeouts) is identical across modes.

    ``dedupe`` (default True) controls whether the EmberVM path attaches the
    Idempotency-Key header, so a webhook redelivery of the same diff collapses
    to the same task. The demo single-scan handler passes ``dedupe=False`` so
    every demo run is a genuinely fresh scan rather than a cached prior result.
    Only the EmberVM dispatch path carries this header; fc-invoke has no
    equivalent, so ``dedupe`` has no effect on the ``fc-invoke``/``shadow`` modes.
    """
    if SEMGREP_DISPATCH == "embervm":
        return await _post_embervm(files, SEMGREP_READ_TIMEOUT, dedupe=dedupe)

    result = await _post_invoke("semgrep", files, SEMGREP_READ_TIMEOUT)

    if SEMGREP_DISPATCH == "shadow" and EMBERVM_URL and files and "error" not in result:
        # Mirror asynchronously; do NOT await (no added latency, no user effect).
        # The event loop keeps the pending task alive until it completes.
        asyncio.ensure_future(_shadow_scan(files, result))

    return result


async def scan_files_hi(files: list[dict]) -> dict:
    """POST a large PR diff to the semgrep-hi fc-invoke workload and return findings.

    Same wire shape and response as ``scan_files``, but targets ``/invoke/semgrep-hi``:
    the warm mcp scan-server sized to 6 vCPU so a many-file diff is matched in
    parallel over cores instead of serially on one. The monolith routes only large
    diffs here (``router._HEAVY_ROUTE_MIN_FILES``); small diffs use ``scan_files``,
    which is faster for them (semgrep-hi carries a fixed thread-pool overhead). Uses
    the same read timeout as ``scan_files`` (a heavy diff scan is still seconds, and
    parallelism keeps it under the daemon's 90s workload timeout).
    """
    return await _post_invoke("semgrep-hi", files, SEMGREP_READ_TIMEOUT)


async def scan_files_full(files: list[dict]) -> dict:
    """POST the whole repo's file contents to the semgrep-full fc-invoke workload.

    Same wire shape as ``scan_files`` (``{"path", "content"}`` entries, same
    daemon response and error shape), but targets ``/invoke/semgrep-full``: a
    separate fc-invoke workload that runs Semgrep's interfile engine over the
    whole repo instead of a per-PR diff. Uses a much longer read timeout
    (``SEMGREP_FULL_READ_TIMEOUT``) since a whole-repo interfile scan is far
    slower than a diff scan.
    """
    return await _post_invoke("semgrep-full", files, SEMGREP_FULL_READ_TIMEOUT)
