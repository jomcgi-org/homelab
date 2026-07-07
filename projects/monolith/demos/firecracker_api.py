"""HTTP API for the firecracker demos page (private tier only).

Wraps the existing firecracker-backed handlers and returns web-shaped payloads
that always carry the captured ``trace_id`` so the frontend can fetch the trace
waterfall:

- ``POST /python``  runs a script in the zero-egress sandbox microVM.
- ``POST /semgrep`` scans supplied files with Semgrep in the fc-invoke workload.
- ``POST /goose``   submits a goosecracker agent run (async, returns immediately).
- ``GET  /goose/{thread_id}`` polls the agent run ledger for that run's state.
- ``GET  /trace/{trace_id}``  returns the SigNoz spans for a captured trace.

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
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from opentelemetry import trace
from opentelemetry.context import Context
from pydantic import BaseModel

import goosecracker.api as goosecracker
from home.observability.traces import fetch_trace_spans
from sandbox.client import run_python_in_sandbox
from semgrep.client import scan_files

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/demos/firecracker", tags=["demos"])

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
    time measured around the call here.
    """
    with _tracer.start_as_current_span("demo.semgrep", context=Context()):
        trace_id = _current_trace_id()
        started = perf_counter()
        result = await scan_files(body.files)
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
    """
    spans = await fetch_trace_spans(trace_id)
    return {"spans": spans, "complete": len(spans) > 0}
