"""HTTP API for the firecracker demos page (private tier only).

Wraps the existing firecracker-backed handlers and returns web-shaped payloads
that always carry the captured ``trace_id`` so the frontend can fetch the trace
waterfall:

- ``POST /python``  runs a script in the zero-egress sandbox microVM.
- ``POST /semgrep`` scans supplied files with Semgrep in the fc-invoke workload.
- ``POST /goose``   submits a goosecracker agent run (async, returns immediately).
- ``GET  /goose/{thread_id}`` polls the agent run ledger for that run's state.
- ``GET  /trace/{trace_id}``  returns the SigNoz spans for a captured trace.
- ``POST /postgres/reset``   destroy the live VM + evict its snapshot (force cold boot).

The demo-postgres status/query/session endpoints moved to ``ember_public``
(mounted at ``/api/ember/postgres`` on both tiers); this module keeps only the
destructive reset verb, which stays private-only (griefing-sensitive).

Each POST wraps its invocation in a fresh ROOT span, detached from any inbound
trace context, via ``context=Context()``. Browser-initiated requests arrive
carrying a ``traceparent`` from the frontend's OTEL fetch instrumentation whose
parent span is never exported to SigNoz; inheriting it makes SigNoz render a
"Missing Span" root and drags the noisy ASGI receive/send spans into the
waterfall. Rooting a new trace per run yields a clean, self-contained trace
(``demo.<kind>`` -> fc-invoke subtree). The trace id is captured as 32 lowercase
hex (matching SigNoz's ``FixedString(32)`` and the ``traces.fetch_trace_spans``
validator). The handlers themselves are reused, not reimplemented: this module
owns no fc-invoke or ClickHouse client of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from opentelemetry import trace
from opentelemetry.context import Context
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

import demos.loadtest as loadtest
import goosecracker.api as goosecracker
from core.db import get_engine
from demos.loadtest_corpus import load_corpus
from ember_public.core import EMBERVM_URL, destroy_demo_pg_instance
from home.observability.traces import fetch_correlated_spans, fetch_trace_spans
from sandbox.client import run_python_in_sandbox
from semgrep_scan.client import scan_files

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/demos/firecracker", tags=["demos"])

# Per-workload VM memory (MiB) recorded in the run config for the frontend.
_WORKLOAD_MEM_MIB = {"semgrep": 1536, "sandbox": 512}

# Agent (goose) run ledger non-terminal states: an active agent run holds
# node-4 capacity, so a load test must not run concurrently with one.
_AGENT_BUSY_STATES = ("RUNNING",)

# Only a RUNNING row this recent counts as a live run. A goose run cannot
# outlive its ~600s requestTimeout, but a crashed run leaves a permanent
# RUNNING tombstone in the ledger; without this bound one orphan would block
# every future load test. 30 minutes is a generous margin over the timeout.
_AGENT_ACTIVE_WINDOW_MIN = 30

# Detached drain tasks are stashed here so the event loop keeps a strong
# reference (an unreferenced task can be GC'd mid-run).
_RUNNING_DRAINS: set[asyncio.Task] = set()

_tracer = trace.get_tracer("demos.firecracker")

# Lifecycle states the agent run ledger stamps as terminal (run finished).
_DONE_STATES = {"COMPLETED", "FAILED"}


def _current_trace_id() -> str:
    """Return the active span's trace id as 32 lowercase hex chars.

    Matches SigNoz's traceID FixedString(32) and the fetch_trace_spans regex.
    An invalid/absent span yields 32 zeros, which is still well-formed hex.
    """
    span = trace.get_current_span()
    return format(span.get_span_context().trace_id, "032x")


class PythonRequest(BaseModel):
    code: str
    files: list[dict] | None = None


class SemgrepRequest(BaseModel):
    files: list[dict]


class GooseRequest(BaseModel):
    task: str
    recipe: str = "agent"
    tier: str = ""


@router.post("/python")
async def run_python(body: PythonRequest) -> dict:
    """Run a Python script in the zero-egress sandbox microVM.

    Returns stdout/stderr/exit_code plus a backend-measured duration and the
    trace id of this invocation. Prefers the daemon's own duration_ms when
    present, falling back to the wall time measured here.
    """
    with _tracer.start_as_current_span("demo.python", context=Context()):
        trace_id = _current_trace_id()
        started = perf_counter()
        result = await run_python_in_sandbox(body.code, body.files)
        elapsed_ms = (perf_counter() - started) * 1000

    return {
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "exit_code": result.get("exit_code"),
        "duration_ms": result.get("duration_ms", elapsed_ms),
        "error": result.get("error"),
        "trace_id": trace_id,
    }


@router.post("/semgrep")
async def run_semgrep(body: SemgrepRequest) -> dict:
    """Scan supplied files with Semgrep in the fc-invoke workload.

    The scan path does not report its own timing, so duration_ms is the wall
    time measured around the call here. Passes ``dedupe=False`` so the demo
    always runs a genuinely fresh scan instead of hitting the idempotency
    result-store cache (the webhook/PR scan path keeps the default dedupe).
    """
    with _tracer.start_as_current_span("demo.semgrep", context=Context()):
        trace_id = _current_trace_id()
        started = perf_counter()
        result = await scan_files(body.files, dedupe=False)
        elapsed_ms = (perf_counter() - started) * 1000

    return {
        "findings": result.get("findings", []),
        "errors": result.get("errors", []),
        "duration_ms": elapsed_ms,
        "error": result.get("error"),
        "trace_id": trace_id,
    }


@router.post("/goose")
async def submit_goose(body: GooseRequest) -> dict:
    """Submit a goosecracker agent run and return immediately.

    The run executes in an isolated microVM off-request; poll
    ``GET /goose/{thread_id}`` for its result. ``session`` is generated here so
    each demo submission is its own run. The submit path is synchronous DB work,
    so it runs off the event loop via ``asyncio.to_thread`` (as the MCP tool does).
    """
    with _tracer.start_as_current_span("demo.goose", context=Context()):
        trace_id = _current_trace_id()
        session = f"demo-{uuid4().hex[:12]}"
        result = await asyncio.to_thread(
            goosecracker.submit,
            body.task,
            session=session,
            recipe=body.recipe,
            tier=body.tier,
            # Default the demo to a checkout of this repo at main so the agent
            # has something real to work on (an empty /workspace makes every
            # "summarize this repo" task impossible). owner/repo form: the runner
            # resolves it to <git-mirror>/jomcgi/homelab (see _effective_mirror_ref).
            repo="jomcgi/homelab",
        )

    return {
        "session": result["session"],
        "thread_id": result["thread_id"],
        "trace_id": trace_id,
    }


@router.get("/goose/{thread_id}")
async def poll_goose(thread_id: str) -> dict:
    """Poll the agent run ledger for one run's state and captured result.

    Returns 404 when the thread id is unknown. ``done`` is true once the run has
    reached a terminal state (COMPLETED or FAILED); a COMPLETED run carries its
    ``result`` and a FAILED run carries its ``result_error``.
    """
    row = await asyncio.to_thread(goosecracker.get_run, thread_id)
    if row is None:
        raise HTTPException(status_code=404, detail="thread not found")

    data = goosecracker.serialize(row)
    state = data.get("state")
    return {
        "status": state,
        "done": state in _DONE_STATES,
        "result": data.get("result"),
        "result_error": data.get("result_error"),
        "thread": data,
    }


@router.get("/trace/{trace_id}")
async def get_trace(trace_id: str) -> dict:
    """Return the SigNoz spans for a captured trace.

    ``complete`` is false while the trace is still ingesting (spans lag emission
    by ~5-10s) or if the trace id is malformed; the frontend polls until spans
    appear.

    ``correlated`` carries the goose agent's own internal spans (service
    `goose-coding`). Goose does not honor an inbound TRACEPARENT, so those spans
    live in their own trace; the runner stamps `caller.trace_id=<trace_id>` onto
    them so we recover them here and render them as their own sub-timeline.
    ``complete`` stays keyed on the main ``spans`` only.
    """
    spans = await fetch_trace_spans(trace_id)
    correlated = await fetch_correlated_spans(trace_id)
    return {"spans": spans, "correlated": correlated, "complete": len(spans) > 0}


# ---------------------------------------------------------------------------
# Load test (workload-parametric drain over the fc-invoke daemon).
# ---------------------------------------------------------------------------


def _running_load_run() -> dict | None:
    """Return the id/started_at of any load_run still 'running', or None.

    Sync; call via asyncio.to_thread.
    """
    with Session(get_engine()) as session:
        row = session.execute(
            text(
                "SELECT id, workload FROM demo.load_run "
                "WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
            )
        ).fetchone()
    return {"id": str(row.id), "workload": row.workload} if row else None


def _agent_is_busy() -> bool:
    """Return True when any goose agent run is in a non-terminal state.

    ASSUMPTION: an active agent run holds node-4 capacity, so a load test must
    not run alongside one. The goosecracker run ledger is
    ``claude_agent.agent_threads``. In the current model the runner only ever
    writes RUNNING (at dispatch) then COMPLETED or FAILED; the older
    PENDING/IDLE lifecycle states are vestigial and never set, so RUNNING is the
    sole active state that matters here.

    Only a RUNNING row updated within ``_AGENT_ACTIVE_WINDOW_MIN`` counts: a
    crashed run leaves a permanent RUNNING tombstone, and without the time bound
    a single orphan would block every future load test. Sync; call via to_thread.
    """
    with Session(get_engine()) as session:
        row = session.execute(
            text(
                "SELECT 1 FROM claude_agent.agent_threads "
                "WHERE state = ANY(:states) "
                "AND last_active_at > now() - make_interval(mins => :window) "
                "LIMIT 1"
            ),
            {"states": list(_AGENT_BUSY_STATES), "window": _AGENT_ACTIVE_WINDOW_MIN},
        ).fetchone()
    return row is not None


def _insert_load_run(workload: str, config: dict) -> str:
    """Insert a running load_run row and return its id. Sync; via to_thread."""
    with Session(get_engine()) as session:
        row = session.execute(
            text(
                """
                INSERT INTO demo.load_run (workload, config)
                VALUES (:workload, CAST(:config AS jsonb))
                RETURNING id
                """
            ),
            {"workload": workload, "config": json.dumps(config)},
        ).fetchone()
        session.commit()
    return str(row.id)


def _load_run_rollup(run_id: str) -> dict | None:
    """Return the run row plus a cheap live rollup over its scans.

    A single aggregate query over demo.load_scan (no per-row fetch) keeps the
    1s poll cheap. Returns None when the run id is unknown. Sync; via to_thread.
    """
    with Session(get_engine()) as session:
        run = session.execute(
            text(
                "SELECT id, workload, started_at, finished_at, duration_s, "
                "status, config, summary FROM demo.load_run WHERE id = :id"
            ),
            {"id": run_id},
        ).fetchone()
        if run is None:
            return None

        agg = session.execute(
            text(
                """
                SELECT
                    count(*) AS total_scans,
                    count(*) FILTER (WHERE status = 'error') AS errors,
                    -- The headline per-scan time is the fc-invoke WALL
                    -- (restore + guest exec), i.e. client wall minus the time
                    -- spent waiting for a daemon slot. Under a saturating drain
                    -- the raw client wall is dominated by that semaphore queue
                    -- (clients / throughput), which measures the drain's own
                    -- oversubscription, not the daemon: the load is artificial,
                    -- so every non-resource number the page reports is aligned
                    -- on this execution time. Raw latency_ms / queue_wait_ms
                    -- stay in demo.load_scan for ad-hoc queries.
                    percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY greatest(latency_ms - coalesce(queue_wait_ms, 0), 0)
                    ) AS latency_p50,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY greatest(latency_ms - coalesce(queue_wait_ms, 0), 0)
                    ) AS latency_p95,
                    avg(cpu_ms) AS cpu_ms_mean,
                    avg(peak_rss_mib) AS peak_rss_mib_mean,
                    extract(epoch FROM (
                        coalesce(max(created_at), now()) - min(created_at)
                    )) AS scan_span_s
                FROM demo.load_scan WHERE run_id = :id
                """
            ),
            {"id": run_id},
        ).fetchone()

        per_lang = session.execute(
            text(
                "SELECT name, count(*) AS c FROM demo.load_scan "
                "WHERE run_id = :id GROUP BY name"
            ),
            {"id": run_id},
        ).fetchall()

    total = agg.total_scans or 0
    # Elapsed against the run's wall clock (started_at -> finished_at or now).
    finished = run.finished_at
    started = run.started_at
    if finished is not None:
        elapsed_s = (finished - started).total_seconds()
    else:
        # scan_span_s is the observed span of recorded scans; a good live proxy.
        elapsed_s = float(agg.scan_span_s or 0.0)
    throughput = (total / elapsed_s) if elapsed_s and elapsed_s > 0 else 0.0
    # In-flight estimate: the drain oversubscribes at client_concurrency but the
    # daemon caps concurrent work at its own concurrency, so while running the
    # steady in-flight count is ~ the daemon concurrency.
    cfg = run.config or {}
    in_flight = cfg.get("daemon_concurrency", 0) if run.status == "running" else 0

    return {
        "run_id": str(run.id),
        "workload": run.workload,
        "status": run.status,
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "duration_s": run.duration_s,
        "config": cfg,
        "elapsed_s": elapsed_s,
        "total_scans": total,
        "errors": agg.errors or 0,
        "throughput_per_s": throughput,
        "in_flight_estimate": in_flight,
        "latency_p50": float(agg.latency_p50) if agg.latency_p50 is not None else None,
        "latency_p95": float(agg.latency_p95) if agg.latency_p95 is not None else None,
        "per_lang_counts": {r.name: r.c for r in per_lang},
        "cpu_ms_mean": float(agg.cpu_ms_mean) if agg.cpu_ms_mean is not None else None,
        "peak_rss_mib_mean": (
            float(agg.peak_rss_mib_mean) if agg.peak_rss_mib_mean is not None else None
        ),
        "summary": run.summary if run.status == "done" else None,
    }


def _load_scans_page(run_id: str, offset: int, limit: int) -> dict:
    """Return a page of scan rows (no ``result`` column) plus a total count.

    Sync; call via asyncio.to_thread.
    """
    with Session(get_engine()) as session:
        total = session.execute(
            text("SELECT count(*) AS c FROM demo.load_scan WHERE run_id = :id"),
            {"id": run_id},
        ).fetchone()
        rows = session.execute(
            text(
                """
                SELECT id, seq, name, status,
                       -- fc-invoke execution wall: client wall minus the drain's
                       -- own oversubscription queue (see _load_run_rollup).
                       greatest(latency_ms - coalesce(queue_wait_ms, 0), 0)
                           AS scan_ms,
                       cpu_ms, peak_rss_mib, result_count
                FROM demo.load_scan
                WHERE run_id = :id
                ORDER BY seq
                OFFSET :offset LIMIT :limit
                """
            ),
            {"id": run_id, "offset": offset, "limit": limit},
        ).fetchall()
    return {
        "total": total.c,
        "offset": offset,
        "limit": limit,
        "scans": [
            {
                "id": r.id,
                "seq": r.seq,
                "name": r.name,
                "status": r.status,
                "scan_ms": r.scan_ms,
                "cpu_ms": r.cpu_ms,
                "peak_rss_mib": r.peak_rss_mib,
                "result_count": r.result_count,
            }
            for r in rows
        ],
    }


def _load_scan_detail(run_id: str, scan_id: int) -> dict | None:
    """Return one scan row WITH its ``result``, or None. Sync; via to_thread."""
    with Session(get_engine()) as session:
        r = session.execute(
            text(
                """
                SELECT id, seq, name, status,
                       greatest(latency_ms - coalesce(queue_wait_ms, 0), 0)
                           AS scan_ms,
                       cpu_ms, peak_rss_mib, result_count, result, error
                FROM demo.load_scan
                WHERE run_id = :id AND id = :scan_id
                """
            ),
            {"id": run_id, "scan_id": scan_id},
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r.id,
        "seq": r.seq,
        "name": r.name,
        "status": r.status,
        "scan_ms": r.scan_ms,
        "cpu_ms": r.cpu_ms,
        "peak_rss_mib": r.peak_rss_mib,
        "result_count": r.result_count,
        "result": r.result,
        "error": r.error,
    }


async def _dispatch_drain(run_id: str, workload: str, duration_s: int) -> None:
    """Run the drain and finalize; log (never raise) on unexpected failure."""
    try:
        store = loadtest.LoadStore(run_id, workload)
        await loadtest.run_load_test(run_id, workload, store, duration_s=duration_s)
    except Exception:  # noqa: BLE001: detached task; surface via logs only
        logger.exception("load-test drain failed for run %s", run_id)


@router.post("/load-test/{workload}")
async def start_load_test(workload: str) -> dict:
    """Start a load-test drain for ``workload`` (semgrep or sandbox).

    Guards: an already-running run short-circuits (returns its id with
    ``already_running``); an active goose agent run refuses with HTTP 409.
    Otherwise inserts a load_run row and dispatches ``run_load_test`` on a
    detached task, returning the new run id immediately.

    The one-run guard is best-effort: the running-check and the insert are
    separate round-trips, so two POSTs racing inside the same ~millisecond could
    both start. That is acceptable for this single-operator demo; if it ever
    needs to be airtight, add a partial unique index on
    ``demo.load_run (status) WHERE status = 'running'`` and treat the
    unique-violation as "already running".
    """
    if workload not in loadtest.WORKLOADS:
        raise HTTPException(status_code=404, detail=f"unknown workload: {workload}")

    running = await asyncio.to_thread(_running_load_run)
    if running is not None:
        return {"run_id": running["id"], "already_running": True}

    if await asyncio.to_thread(_agent_is_busy):
        raise HTTPException(
            status_code=409,
            detail="an agent run is active; load test refused to avoid "
            "contending for node-4 capacity",
        )

    corpus = load_corpus(workload)
    config = {
        "workload": workload,
        "daemon_concurrency": loadtest.DAEMON_CONCURRENCY,
        "client_concurrency": 32,
        "vcpus": loadtest.VCPUS_PER_SCAN,
        "mem_mib": _WORKLOAD_MEM_MIB[workload],
        "node": loadtest.SAMPLE_NODE,
        "corpus": [c["name"] for c in corpus],
    }
    run_id = await asyncio.to_thread(_insert_load_run, workload, config)

    task = asyncio.create_task(_dispatch_drain(run_id, workload, 60))
    _RUNNING_DRAINS.add(task)
    task.add_done_callback(_RUNNING_DRAINS.discard)

    return {"run_id": run_id}


@router.get("/load-test/{run_id}")
async def get_load_test(run_id: str) -> dict:
    """Return the run row + a cheap live rollup (and summary when done)."""
    rollup = await asyncio.to_thread(_load_run_rollup, run_id)
    if rollup is None:
        raise HTTPException(status_code=404, detail="run not found")
    return rollup


@router.get("/load-test/{run_id}/scans")
async def get_load_test_scans(run_id: str, offset: int = 0, limit: int = 50) -> dict:
    """Return a paginated page of scan rows (without the ``result`` column)."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return await asyncio.to_thread(_load_scans_page, run_id, offset, limit)


@router.get("/load-test/{run_id}/scans/{scan_id}")
async def get_load_test_scan(run_id: str, scan_id: int) -> dict:
    """Return one scan row WITH its ``result`` payload."""
    detail = await asyncio.to_thread(_load_scan_detail, run_id, scan_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return detail


# ---------------------------------------------------------------------------
# Demo-postgres reset (embervm R4 stateful sleep/wake exhibit).
#
# Status/query/session moved to ember_public (mounted at /api/ember/postgres
# on both tiers); this destructive, griefing-sensitive verb stays private-only:
#   reset -> DELETE {EMBERVM_URL}/v1/stateful/demo-postgres/instance (destroys
#            the live VM AND evicts the banked bundle; the volume survives, so
#            the next connect cold-boots against retained data)
# ---------------------------------------------------------------------------


@router.post("/postgres/reset")
async def postgres_reset() -> dict:
    """Force the next wake down the cold-boot path.

    DELETE .../instance destroys the live VM (if any) AND evicts the banked
    bundle; the volume is untouched. The next connect therefore cold-boots
    Postgres against the retained data: the demo's way of producing the
    cold-start number (and proving durability) on demand.
    """
    if not EMBERVM_URL:
        raise HTTPException(status_code=503, detail="EMBERVM_URL is not configured")
    data = await destroy_demo_pg_instance()
    return {
        "destroyed": data.get("destroyed", 0),
        "evicted": data.get("evicted", 0),
    }
