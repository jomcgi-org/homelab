"""Synthetic probes for the public Ember demos.

These run in the PRIVATE API pod, driven by synthetic_router's internal
endpoint, which the ember-synthetic CronWorkflow only triggers. The API pod is
where the admitted ServiceAccount, EmberVM credentials and DEMO_POSTGRES_DSN live;
running them in the ephemeral job pod instead is what broke the #4065 rollout.
Each probe returns its failure in-band as {ok, detail, latency_ms} and never
raises, because a crashing probe IS the finding.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from time import perf_counter

import httpx
from sqlmodel import Session

from core.db import get_engine
from ember_public import bazel_core, core, semgrep_core
from ember_public.synthetic_models import EmberSyntheticProbe

logger = logging.getLogger(__name__)

# These sentinels are deliberately <title> strings, which stay stable across
# site rewording. They remain a real signal because SvelteKit SSR failure
# renders an error page with a completely different title, so a title mismatch
# is both reliable and meaningful.
PAGE_SENTINELS = {
    "/ember": "Ember · a workload orchestrator on Firecracker microVMs",
    "/ember/firecracker": "Boot once, restore forever",
    "/ember/postgres": "Ember Postgres",
    "/ember/bazel": "Ember Bazel Skyframe Query",
    "/ember/semgrep": "Ember Semgrep",
}

# A real control-plane outage persists and still latches red on its first
# probe run; #4137 lasted over an hour. A CP roll takes 15 to 60s, so this
# absorbs normal Recreate downtime without blunting outage detection. The 90s
# ceiling leaves 90s of margin inside the CronWorkflow's documented 180s API
# request timeout (all four probes run concurrently) and stays below its 300s
# deadline, so runs cannot overlap.
EMBER_SYNTHETIC_RETRY_BUDGET_S = float(
    os.environ.get("EMBER_SYNTHETIC_RETRY_BUDGET_S", "90.0")
)
EMBER_SYNTHETIC_RETRY_INTERVAL_S = 15.0


def _failure(exc: Exception) -> dict:
    return {"ok": False, "detail": str(exc), "latency_ms": None}


async def _retry_probe(probe) -> dict:
    started = perf_counter()
    retries = 0
    result = await probe()
    while not result["ok"]:
        elapsed = perf_counter() - started
        if elapsed + EMBER_SYNTHETIC_RETRY_INTERVAL_S >= EMBER_SYNTHETIC_RETRY_BUDGET_S:
            break
        await asyncio.sleep(EMBER_SYNTHETIC_RETRY_INTERVAL_S)
        result = await probe()
        retries += 1
    if result["ok"] and retries:
        result = dict(result)
        result["detail"] += f" (recovered after {retries} retries)"
    return result


async def _probe_bazel_once() -> dict:
    # A missing warmth marker is a FAILURE even though bazel_core._check_drift
    # only logs a WARNING for the same condition. The query still returns 200
    # OK, so a passive check stays green while the exhibit's premise, that
    # Skyframe is already warm and nothing was re-analyzed, is dead. Reaching
    # for the private _ANALYZED_LINE_OK_MARKER is deliberate so this probe and
    # the drift check can never disagree about the marker text.
    try:
        status, payload = await bazel_core.run_query("deps(//absl/strings)")
        if status != 200:
            return {
                "ok": False,
                "detail": f"status {status}: {payload.get('error', payload)}",
                "latency_ms": payload.get("wall_ms"),
            }
        marker = bazel_core._ANALYZED_LINE_OK_MARKER
        analyzed_line = payload.get("analyzed_line", "")
        if marker not in analyzed_line:
            return {
                "ok": False,
                "detail": f"missing warmth marker {marker!r}: {analyzed_line}",
                "latency_ms": payload.get("wall_ms"),
            }
        return {
            "ok": True,
            "detail": f"warm, {payload.get('wall_ms')}ms",
            "latency_ms": payload.get("wall_ms"),
        }
    except Exception as exc:  # noqa: BLE001 - probes report failures in-band
        return _failure(exc)


async def probe_bazel() -> dict:
    return await _retry_probe(_probe_bazel_once)


async def _probe_semgrep_once() -> dict:
    try:
        from semgrep_scan.client import scan_files

        # This is the sample actually verified against the deployed scanner,
        # so it is known to fire. It exercises interprocedural analysis:
        # request.args.get("tool") flows through build_command() into os.system().
        # A simplified single-file version could fire under a basic ruleset
        # while interprocedural capability was dead, the same blind spot the
        # bazel warmth marker exists to close.
        snippet = """import os

from flask import Flask, request

app = Flask(__name__)


def build_command():
    tool = request.args.get("tool")
    return f"/usr/bin/{tool} --report"


@app.route("/run")
def run():
    os.system(build_command())
    return "started"
"""
        started = perf_counter()
        result = await scan_files(
            [{"path": semgrep_core.snippet_path("python"), "content": snippet}],
            dedupe=False,
        )
        if result.get("error"):
            return {
                "ok": False,
                "detail": str(result["error"]),
                "latency_ms": (perf_counter() - started) * 1000,
            }
        # Empty findings can mean the scanner loaded no rules, a silent failure.
        if len(result.get("findings", [])) < 1:
            return {
                "ok": False,
                "detail": "scanner returned no findings",
                "latency_ms": (perf_counter() - started) * 1000,
            }
        return {
            "ok": True,
            "detail": f"{len(result['findings'])} finding(s)",
            "latency_ms": (perf_counter() - started) * 1000,
        }
    except Exception as exc:  # noqa: BLE001 - probes report failures in-band
        return _failure(exc)


async def probe_semgrep() -> dict:
    return await _retry_probe(_probe_semgrep_once)


async def probe_pages() -> dict:
    base = os.environ.get("EMBER_SYNTHETIC_BASE_URL", "").rstrip("/")
    if not base:
        return {"ok": True, "detail": "not configured", "latency_ms": None}
    started = perf_counter()
    # This deliberately goes out over the public internet rather than to the
    # monolith-public Service in-cluster: it covers Cloudflare, the HTTPRoute
    # and SSR the way a visitor experiences them, and monolith-public's Cilium
    # ingress policies would drop a cross-namespace fetch anyway. It is the one
    # probe that still exercises the public edge now that the others call their
    # cores in-process. The single retry with 0.2s backoff keeps one Cloudflare
    # blip from latching the demo down for a whole five-minute cadence.
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for path, sentinel in PAGE_SENTINELS.items():
                response = None
                for attempt in range(2):
                    try:
                        response = await client.get(f"{base}{path}")
                    except httpx.TransportError as exc:
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                            continue
                        return {
                            "ok": False,
                            "detail": f"{path}: transport error: {exc}",
                            "latency_ms": (perf_counter() - started) * 1000,
                        }
                    if response.status_code >= 500 and attempt == 0:
                        await asyncio.sleep(0.2)
                        continue
                    break
                if response is None or response.status_code != 200:
                    status = (
                        response.status_code if response is not None else "no response"
                    )
                    return {
                        "ok": False,
                        "detail": f"{path}: status {status}",
                        "latency_ms": (perf_counter() - started) * 1000,
                    }
                if sentinel not in response.text:
                    return {
                        "ok": False,
                        "detail": f"{path}: missing sentinel {sentinel!r}",
                        "latency_ms": (perf_counter() - started) * 1000,
                    }
        return {
            "ok": True,
            "detail": f"all pages, {(perf_counter() - started) * 1000:.0f}ms",
            "latency_ms": (perf_counter() - started) * 1000,
        }
    except Exception as exc:  # noqa: BLE001 - probes report failures in-band
        return _failure(exc)


async def _probe_postgres_once() -> dict:
    """Probe via a direct core call, not HTTP.

    Aggregate is read-only (same reads without writing, proving wake needs no
    write), so the probe never appends to the visitor ledger. It respects the
    query slot semaphore so it never starves real visitors.
    """
    dsn = core.demo_pg_dsn()
    if not dsn:
        # This used to fail open because demo_postgres caught the unconfigured
        # case. With that check gone, a misconfigured deploy would otherwise
        # read green.
        return {
            "ok": False,
            "detail": "DEMO_POSTGRES_DSN not configured",
            "latency_ms": None,
        }

    before = None
    if core.EMBERVM_URL:
        try:
            before = await core.cached_demo_pg_status()
        except Exception as exc:  # noqa: BLE001 - classification is best-effort
            logger.warning("demo-postgres synthetic pre-query status failed: %s", exc)
            before = None

    if not core.try_acquire_query_slot():
        return {
            "ok": True,
            "detail": "busy, skipped",
            "latency_ms": None,
            "skip": True,
        }

    try:
        result = await asyncio.to_thread(
            core.demo_pg_orders_roundtrip, dsn, "aggregate", None
        )
        return {
            "ok": True,
            "detail": f"{core.classify_wake(before)}, {result.get('connect_ms')}ms",
            "latency_ms": result.get("connect_ms"),
        }
    except Exception as exc:  # noqa: BLE001 - probes report failures in-band
        logger.warning("demo-postgres synthetic roundtrip failed: %s", exc)
        return _failure(exc)
    finally:
        core.release_query_slot()


async def probe_postgres() -> dict:
    return await _retry_probe(_probe_postgres_once)


async def probe_codex() -> dict:
    """Exercise a real Codex lane session through the Luna model."""
    started = perf_counter()
    try:
        from agent_sessions.api import run_synthetic_session
        from agent_sessions.constants import CODEX_SYNTHETIC_PROMPT

        turn = await run_synthetic_session(
            CODEX_SYNTHETIC_PROMPT,
            model="luna",
        )
        if turn.terminal_reason not in {"completed", "stop"}:
            return {
                "ok": False,
                "detail": f"turn reason {turn.terminal_reason!r}",
                "latency_ms": (perf_counter() - started) * 1000,
            }
        if not turn.result.strip():
            return {
                "ok": False,
                "detail": "completed turn had an empty result",
                "latency_ms": (perf_counter() - started) * 1000,
            }
        return {
            "ok": True,
            "detail": f"completed, destroyed, {(perf_counter() - started) * 1000:.0f}ms",
            "latency_ms": (perf_counter() - started) * 1000,
        }
    except Exception as exc:  # noqa: BLE001 - probes report failures in-band
        return _failure(exc)


def _record_sync(demo: str, result: dict) -> None:
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:  # jobs use the private app-role DATABASE_URL
        row = session.get(EmberSyntheticProbe, demo)
        if row is None:
            row = EmberSyntheticProbe(
                demo=demo,
                ok=result["ok"],
                detail=result["detail"],
                latency_ms=result.get("latency_ms"),
                checked_at=now,
                last_ok_at=now if result["ok"] else None,
            )
            session.add(row)
        else:
            row.ok = result["ok"]
            row.detail = result["detail"]
            row.latency_ms = result.get("latency_ms")
            row.checked_at = now
            if result["ok"]:
                row.last_ok_at = now
        session.commit()


async def record(demo: str, result: dict) -> None:
    if result.get("skip"):
        return
    await asyncio.to_thread(_record_sync, demo, result)
