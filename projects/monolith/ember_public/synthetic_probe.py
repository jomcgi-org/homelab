"""Synthetic probes for the Ember agent lanes.

These run in the PRIVATE API pod, driven by synthetic_router's internal
endpoints, which the ember-*-session-synthetic CronWorkflows only trigger. The
API pod is where the admitted ServiceAccount and the EmberVM credentials live;
running them in the ephemeral job pod instead is what broke the #4065 rollout.
Each probe returns its failure in-band as {ok, detail, latency_ms} and never
raises, because a crashing probe IS the finding.

The demo probes (bazel, pages, postgres) left with the public Ember exhibits
(#6913); only the agent session probes remain.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter

from core.db import get_engine
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode
from sqlmodel import Session

from ember_public.synthetic_models import EmberSyntheticProbe

logger = logging.getLogger(__name__)

_tracer = trace.get_tracer("ember.synthetic_probe")

_SPAN_DETAIL_MAX_CHARS = 512


def _failure(
    exc: Exception,
    *,
    trace_id: str | None = None,
    ember_session_id: str | None = None,
) -> dict:
    return {
        "ok": False,
        "detail": str(exc),
        "latency_ms": None,
        "trace_id": trace_id,
        "ember_session_id": ember_session_id,
    }


def _current_trace_id() -> str | None:
    """Return the active valid W3C trace ID, or None without tracing."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


def _correlate(
    result: dict,
    trace_id: str | None,
    ember_session_id: str | None = None,
) -> dict:
    correlated = dict(result)
    correlated["trace_id"] = trace_id
    correlated["ember_session_id"] = ember_session_id
    return correlated


def _finish_root(span, demo: str, result: dict) -> None:
    """Record the final outcome on an ember.probe.<demo> root span."""
    span.set_attributes(
        {
            "ember.probe.demo": demo,
            "ember.probe.ok": result["ok"],
            "ember.probe.detail": result["detail"][:_SPAN_DETAIL_MAX_CHARS],
        }
    )
    if not result["ok"]:
        span.set_status(Status(StatusCode.ERROR))


async def probe_codex() -> dict:
    """Exercise a real Codex lane session through the Luna model."""
    return await _probe_session("codex", "luna")


async def probe_spark() -> dict:
    """Exercise a real Muse-family session on claude-runtime."""
    return await _probe_session("spark", "spark")


async def _run_session_probe(demo: str, model: str, trace_id: str | None) -> dict:
    started = perf_counter()
    ember_session_id = None

    def capture_ember_session_id(session_id: str) -> None:
        nonlocal ember_session_id
        ember_session_id = session_id

    try:
        from factory.execution.api import run_synthetic_session

        if demo == "codex":
            from factory.execution.constants import CODEX_SYNTHETIC_PROMPT

            prompt = CODEX_SYNTHETIC_PROMPT
        elif demo == "spark":
            from factory.execution.constants import SPARK_SYNTHETIC_PROMPT

            prompt = SPARK_SYNTHETIC_PROMPT
        else:
            raise ValueError(f"unknown session probe {demo!r}")

        turn = await run_synthetic_session(
            prompt,
            model=model,
            on_ember_session_id=capture_ember_session_id,
        )
        if turn is None:
            # Another replica claimed this run's pending message and is
            # delivering it. Nothing was proven, but nothing is known broken
            # either, so report ok rather than paging.
            return _correlate(
                {
                    "ok": True,
                    "detail": "another replica delivered this run",
                    "latency_ms": (perf_counter() - started) * 1000,
                },
                trace_id,
                ember_session_id,
            )
        if turn.terminal_reason not in {"completed", "stop"}:
            return _correlate(
                {
                    "ok": False,
                    "detail": f"turn reason {turn.terminal_reason!r}",
                    "latency_ms": (perf_counter() - started) * 1000,
                },
                trace_id,
                ember_session_id,
            )
        if not turn.result.strip():
            return _correlate(
                {
                    "ok": False,
                    "detail": "completed turn had an empty result",
                    "latency_ms": (perf_counter() - started) * 1000,
                },
                trace_id,
                ember_session_id,
            )
        elapsed_ms = (perf_counter() - started) * 1000
        return _correlate(
            {
                "ok": True,
                "detail": f"completed, destroyed, {elapsed_ms:.0f}ms",
                "latency_ms": elapsed_ms,
            },
            trace_id,
            ember_session_id,
        )
    except Exception as exc:  # noqa: BLE001 - probes report failures in-band
        return _failure(
            exc,
            trace_id=trace_id,
            ember_session_id=ember_session_id,
        )


async def _probe_session(demo: str, model: str) -> dict:
    """Run one session synthetic under an independent trace root."""
    with _tracer.start_as_current_span(
        f"ember.probe.{demo}", context=Context()
    ) as span:
        result = await _run_session_probe(demo, model, _current_trace_id())
        _finish_root(span, demo, result)
        return result


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
                trace_id=result.get("trace_id"),
                ember_session_id=result.get("ember_session_id"),
                checked_at=now,
                last_ok_at=now if result["ok"] else None,
            )
            session.add(row)
        else:
            row.ok = result["ok"]
            row.detail = result["detail"]
            row.latency_ms = result.get("latency_ms")
            row.trace_id = result.get("trace_id")
            row.ember_session_id = result.get("ember_session_id")
            row.checked_at = now
            if result["ok"]:
                row.last_ok_at = now
        session.commit()


async def record(demo: str, result: dict) -> None:
    if result.get("skip"):
        return
    await asyncio.to_thread(_record_sync, demo, result)
