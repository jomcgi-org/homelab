"""Workload-parametric load-test drain for the firecracker demos page.

Drains a corpus of example files through the fc-invoke daemon (semgrep or
sandbox workload) as fast as the daemon's per-workload semaphore allows, for a
fixed run duration. It:

- oversubscribes the daemon (``client_concurrency`` = 32 > daemon concurrency
  16) so the daemon's semaphore stays saturated and throughput is daemon-bound;
- buffers per-scan rows and writes them to ``demo.load_scan`` in batched
  multi-row INSERTs (via a sync engine off the event loop);
- samples the fc-invoke pod and node-4 resource footprint every ~2s
  best-effort; and
- computes an aggregate summary (percentiles, per-language breakdown, daemon +
  node footprint, and a per-node/per-core throughput extrapolation) into
  ``demo.load_run.summary``.

All DB work is synchronous SQLAlchemy (``core.db`` engine) run in a worker
thread via ``asyncio.to_thread`` so the drain never blocks the event loop. The
drain logic is unit-testable without a real DB by injecting a fake ``_invoke``
and an in-memory store subclass (see ``loadtest_test.py``).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import time
from time import perf_counter
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import text
from sqlmodel import Session

from core.db import get_engine
from cluster.api import KubernetesClient
from demos.loadtest_corpus import load_corpus
from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

# The daemon runs each workload at concurrency 16 on node-4, one vcpu per VM.
# MUST match `workloads.{semgrep,sandbox}.concurrency` in
# projects/firecracker/substrate/chart/values.yaml: this constant feeds the
# scans/core/s extrapolation, so drift silently skews that number.
DAEMON_CONCURRENCY = 16
VCPUS_PER_SCAN = 1
FC_INVOKE_NAMESPACE = "monolith"
# The fc-invoke pod name carries this prefix; pod-metrics lookup matches on it.
FC_INVOKE_POD_PREFIX = "fc-invoke"
SAMPLE_NODE = "node-4"

# Auto-flush the scan buffer once it reaches this many rows (one INSERT).
_FLUSH_THRESHOLD = 200

# Also flush at least this often so the 1s live poll sees fresh rows promptly
# instead of only at 200-row batch boundaries (which, at tens of scans/s, is a
# multi-second lag where the live view looks frozen right after kickoff).
_FLUSH_INTERVAL_S = 1.0

# Grace window past the drain deadline for in-flight invokes to finish before
# stragglers are cancelled. A normal scan is ~1s, so this lets legitimately
# in-flight scans complete while bounding a wedged run (the run row is the
# one-run lock, so an unbounded drain would block the next run for up to a full
# read_timeout). Total drain is thus capped at duration_s + _DRAIN_GRACE_S.
_DRAIN_GRACE_S = 10


def _parse_semgrep_result(resp: dict) -> tuple[dict, int | None]:
    findings = resp.get("findings", []) or []
    errors = resp.get("errors", []) or []
    return {"findings": findings, "errors": errors}, len(findings)


def _parse_sandbox_result(resp: dict) -> tuple[dict, int | None]:
    return (
        {
            "stdout": resp.get("stdout", ""),
            "stderr": resp.get("stderr", ""),
            "exit_code": resp.get("exit_code"),
            "duration_ms": resp.get("duration_ms"),
            "truncated": resp.get("truncated"),
        },
        None,
    )


# Workload registry: each workload knows how to build its /invoke payload from a
# corpus item, how to parse a 2xx response into (result_jsonb, result_count),
# and its read timeout (a bit past the daemon's per-workload request budget).
WORKLOADS: dict[str, dict[str, Any]] = {
    "semgrep": {
        "build_payload": lambda item: {
            "files": [{"path": item["path"], "content": item["content"]}]
        },
        "parse_result": _parse_semgrep_result,
        "read_timeout": 95.0,
    },
    "sandbox": {
        "build_payload": lambda item: {"code": item["content"]},
        "parse_result": _parse_sandbox_result,
        "read_timeout": 40.0,
    },
}


def _parse_int_header(value: str | None) -> int | None:
    """Parse an X-Fc-* integer header, returning None when absent/unparseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _invoke(
    workload: str, payload: dict, timeout: float
) -> tuple[dict, dict, str | None]:
    """POST one payload to ``/invoke/{workload}`` and return (json, meta, error).

    On any 2xx: returns the decoded JSON plus a ``meta`` dict carrying the
    daemon's per-scan resource headers (``cpu_ms``, ``peak_rss_mib``,
    ``queue_wait_ms``), each an int or None. On ANY failure (bad status,
    connect, timeout, decode, unexpected exception) it returns
    ``({}, {}, "<error>")`` and never raises, so one bad scan cannot kill a
    worker.
    """
    if not FC_INVOKE_URL:
        return {}, {}, "FC_INVOKE_URL is not configured"

    client_timeout = httpx.Timeout(timeout, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            resp = await client.post(
                f"{FC_INVOKE_URL}/invoke/{workload}",
                json=payload,
                headers=auth_headers(),
            )
            resp.raise_for_status()
            meta = {
                "cpu_ms": _parse_int_header(resp.headers.get("X-Fc-Cpu-Ms")),
                "peak_rss_mib": _parse_int_header(
                    resp.headers.get("X-Fc-Peak-Rss-Mib")
                ),
                "queue_wait_ms": _parse_int_header(
                    resp.headers.get("X-Fc-Queue-Wait-Ms")
                ),
            }
            return resp.json(), meta, None
    except httpx.HTTPStatusError as exc:
        return {}, {}, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        # Not swallowed: the failure is surfaced to the caller as the error
        # element of the return tuple and recorded as a status='error' scan row.
        # Logged at debug so a saturated drain's thousands of expected
        # connect/timeout errors do not flood the log at the default level.
        logger.debug("load-test invoke failed: %s", exc)
        return {}, {}, f"{type(exc).__name__}: {exc}"


class LoadStore:
    """Buffered writer + summary finalizer over ``demo.load_scan`` / ``load_run``.

    ``record`` appends a scan row and flushes when the buffer fills; ``finalize``
    flushes the tail, computes the summary, and stamps the run done. All DB work
    is synchronous SQLAlchemy run off the event loop via ``asyncio.to_thread``.
    Tests use ``FakeLoadStore`` (below) to exercise the drain with no DB.
    """

    def __init__(self, run_id: str, workload: str) -> None:
        self.run_id = run_id
        self.workload = workload
        self._buffer: list[dict] = []
        # Every recorded row, retained for the in-process summary computation so
        # finalize does not have to re-read them from Postgres.
        self._all_rows: list[dict] = []
        self._sampler_series: list[dict] = []
        self._lock = asyncio.Lock()
        self._last_flush = time.monotonic()

    def set_sampler_series(self, series: list[dict]) -> None:
        self._sampler_series = series

    async def record(self, row: dict) -> None:
        """Append a scan row; flush the buffer to Postgres when it fills or ages.

        Flushes on either the size threshold or the time interval, so the 1s live
        poll sees rows within ~1s of a run starting, not only once 200 rows have
        accumulated.
        """
        async with self._lock:
            self._all_rows.append(row)
            self._buffer.append(row)
            due = (time.monotonic() - self._last_flush) >= _FLUSH_INTERVAL_S
            if len(self._buffer) >= _FLUSH_THRESHOLD or due:
                batch, self._buffer = self._buffer, []
                self._last_flush = time.monotonic()
                await asyncio.to_thread(self._flush_batch, batch)

    async def flush(self) -> None:
        """Flush any buffered rows to Postgres in one INSERT."""
        async with self._lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer, []
        await asyncio.to_thread(self._flush_batch, batch)

    def _flush_batch(self, batch: list[dict]) -> None:
        """One multi-row INSERT into demo.load_scan. Sync; runs in a thread."""
        if not batch:
            return
        params = [
            {
                "run_id": self.run_id,
                "workload": self.workload,
                "seq": r.get("seq"),
                "name": r.get("name"),
                "status": r.get("status"),
                "latency_ms": r.get("latency_ms"),
                "queue_wait_ms": r.get("queue_wait_ms"),
                "cpu_ms": r.get("cpu_ms"),
                "peak_rss_mib": r.get("peak_rss_mib"),
                "result_count": r.get("result_count"),
                "result": json.dumps(r.get("result"))
                if r.get("result") is not None
                else None,
                "error": r.get("error"),
            }
            for r in batch
        ]
        with Session(get_engine()) as session:
            session.execute(
                text(
                    """
                    INSERT INTO demo.load_scan
                        (run_id, workload, seq, name, status, latency_ms,
                         queue_wait_ms, cpu_ms, peak_rss_mib, result_count,
                         result, error)
                    VALUES
                        (:run_id, :workload, :seq, :name, :status, :latency_ms,
                         :queue_wait_ms, :cpu_ms, :peak_rss_mib, :result_count,
                         CAST(:result AS jsonb), :error)
                    """
                ),
                params,
            )
            session.commit()

    async def finalize(self, run_id: str) -> dict:
        """Flush the tail, compute the summary, and mark the run done."""
        await self.flush()
        summary = build_summary(self.workload, self._all_rows, self._sampler_series)
        await asyncio.to_thread(self._write_summary, run_id, summary)
        return summary

    def _write_summary(self, run_id: str, summary: dict) -> None:
        """Stamp the run done with its summary. Sync; runs in a thread."""
        with Session(get_engine()) as session:
            session.execute(
                text(
                    """
                    UPDATE demo.load_run
                       SET finished_at = now(),
                           status = 'done',
                           summary = CAST(:summary AS jsonb)
                     WHERE id = :id
                    """
                ),
                {"summary": json.dumps(summary), "id": run_id},
            )
            session.commit()


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Simple sorted-index percentile (q in [0,1]); None on empty input."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _pct_block(values: list[float], qs: tuple[float, ...]) -> dict:
    s = sorted(v for v in values if v is not None)
    labels = {0.5: "p50", 0.95: "p95", 1.0: "max"}
    return {labels[q]: _percentile(s, q) for q in qs}


def _mean_max(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "max": None}
    return {"mean": sum(vals) / len(vals), "max": max(vals)}


def build_summary(workload: str, rows: list[dict], sampler_series: list[dict]) -> dict:
    """Aggregate scan rows + sampler series into the run summary jsonb.

    Percentiles are a simple sorted-index estimate (no numpy dependency). When
    the sampler series is empty (metrics unavailable), ``daemon.source`` is
    ``"derived"`` and daemon RSS is estimated from per-scan peak RSS; node stats
    are omitted.
    """
    total = len(rows)
    errors = sum(1 for r in rows if r.get("status") == "error")
    ok_rows = [r for r in rows if r.get("status") == "ok"]

    latencies = [r.get("latency_ms") for r in rows if r.get("latency_ms") is not None]
    wall_s = (sum(latencies) / 1000.0) if latencies else 0.0
    # Wall time of the run: span from the drain, approximated by max scan
    # finish. We store throughput against the configured duration downstream;
    # here wall_s is the summed VM-seconds proxy used for extrapolation below.

    # Actual run wall clock comes from the run row's timestamps at query time;
    # for the summary we use total / duration via the throughput field the
    # caller fills. We compute throughput from total scans over the observed
    # span of the sampler (best proxy) or fall back to summed latencies.
    observed_wall = None
    if sampler_series:
        ts = [s.get("t") for s in sampler_series if s.get("t") is not None]
        if len(ts) >= 2:
            observed_wall = max(ts) - min(ts)
    run_wall_s = observed_wall if observed_wall and observed_wall > 0 else wall_s
    throughput = (total / run_wall_s) if run_wall_s and run_wall_s > 0 else 0.0

    # Headline latency is the fc-invoke WALL per scan (client wall minus the
    # time spent queued for a daemon slot): the real per-scan cost, not the
    # oversubscription queue. The queue is reported separately in queue_wait_ms.
    def _exec_ms(r: dict) -> float:
        return max(float(r["latency_ms"]) - float(r.get("queue_wait_ms") or 0), 0.0)

    lat_vals = [_exec_ms(r) for r in rows if r.get("latency_ms") is not None]
    queue_vals = [
        float(r["queue_wait_ms"]) for r in rows if r.get("queue_wait_ms") is not None
    ]
    cpu_vals = [float(r["cpu_ms"]) for r in rows if r.get("cpu_ms") is not None]
    rss_vals = [
        float(r["peak_rss_mib"]) for r in rows if r.get("peak_rss_mib") is not None
    ]

    per_lang: dict[str, dict] = {}
    names = {r.get("name") for r in rows if r.get("name") is not None}
    for name in names:
        group = [r for r in rows if r.get("name") == name]
        g_lat = sorted(_exec_ms(r) for r in group if r.get("latency_ms") is not None)
        g_cpu = sorted(float(r["cpu_ms"]) for r in group if r.get("cpu_ms") is not None)
        per_lang[name] = {
            "count": len(group),
            "p50_ms": _percentile(g_lat, 0.5),
            "p50_cpu_ms": _percentile(g_cpu, 0.5),
        }

    summary: dict[str, Any] = {
        "total_scans": total,
        "errors": errors,
        "wall_s": run_wall_s,
        "throughput_per_s": throughput,
        "latency_ms": _pct_block(lat_vals, (0.5, 0.95, 1.0)),
        "queue_wait_ms": _pct_block(queue_vals, (0.5, 0.95)),
        "per_scan_cpu_ms": _pct_block(cpu_vals, (0.5, 0.95)),
        "per_scan_peak_rss_mib": _pct_block(rss_vals, (0.5, 0.95)),
        "per_lang": per_lang,
    }

    # Workload-specific result signal: semgrep reports finding counts, sandbox
    # reports its exit-code distribution.
    if workload == "sandbox":
        ok_exit = sum(
            1 for r in ok_rows if (r.get("result") or {}).get("exit_code") == 0
        )
        nonzero = sum(
            1
            for r in ok_rows
            if (r.get("result") or {}).get("exit_code") not in (0, None)
        )
        summary["sandbox_exit"] = {"ok_count": ok_exit, "nonzero_count": nonzero}
    else:
        rc = [
            float(r["result_count"]) for r in rows if r.get("result_count") is not None
        ]
        summary["result_count"] = _pct_block(rc, (0.5, 0.95, 1.0))

    # Daemon + node footprint from the sampler, or a derived estimate.
    if sampler_series:
        summary["daemon"] = {
            "pod_cpu_m": _mean_max([s.get("pod_cpu_m") for s in sampler_series]),
            "pod_rss_mib": _mean_max([s.get("pod_rss_mib") for s in sampler_series]),
            "source": "metrics",
        }
        summary["node"] = {
            "cpu_m": _mean_max([s.get("node_cpu_m") for s in sampler_series]),
            "rss_mib": _mean_max([s.get("node_rss_mib") for s in sampler_series]),
        }
    else:
        # No live metrics: estimate daemon RSS as the worst single-scan peak
        # times the daemon concurrency (approximates the max concurrent
        # footprint), and omit node stats.
        max_scan_rss = max(rss_vals) if rss_vals else None
        derived_rss = (
            max_scan_rss * DAEMON_CONCURRENCY if max_scan_rss is not None else None
        )
        summary["daemon"] = {
            "pod_cpu_m": {"mean": None, "max": None},
            "pod_rss_mib": {"mean": derived_rss, "max": derived_rss},
            "source": "derived",
        }

    # Extrapolation: throughput is core-bound at ~scans/core/s; N nodes scale
    # roughly linearly minus fixed per-node daemon overhead.
    concurrency_cores = DAEMON_CONCURRENCY * VCPUS_PER_SCAN
    scans_per_core_s = throughput / concurrency_cores if concurrency_cores else 0.0
    vm_seconds = wall_s  # sum of latency_ms / 1000 across all scans
    summary["extrapolation"] = {
        "per_node_throughput_per_s": throughput,
        "scans_per_core_s": scans_per_core_s,
        "vm_seconds": vm_seconds,
        "note": (
            f"Throughput is core-bound at ~{scans_per_core_s:.2f} scans/core/s; "
            f"N nodes ~= N x {throughput:.2f} per_node_throughput_per_s (minus "
            "fixed daemon overhead per node)."
        ),
    }
    return summary


def _pod_cpu_milli_and_rss_mib(pod_metric: dict) -> tuple[float, float]:
    """Sum a pod-metrics object's container cpu (millicores) and rss (MiB)."""
    from cluster.kubernetes import _parse_cpu, _parse_memory  # reuse parsers

    cpu_cores = 0.0
    rss_bytes = 0.0
    for c in pod_metric.get("containers", []) or []:
        usage = c.get("usage", {}) or {}
        cpu_cores += _parse_cpu(usage.get("cpu", "0"))
        rss_bytes += _parse_memory(usage.get("memory", "0"))
    return cpu_cores * 1000.0, rss_bytes / (1024**2)


async def sample_resources(
    stop_event: asyncio.Event, client: KubernetesClient | None = None
) -> list[dict]:
    """Sample fc-invoke pod + node-4 footprint every ~2s until ``stop_event``.

    Best-effort: any sampling failure (RBAC/API error) is logged once and the
    loop keeps going, returning whatever series was collected (possibly empty).
    """
    series: list[dict] = []
    kube = client or KubernetesClient()
    owns_client = client is None
    logged_error = False
    start = time.monotonic()
    try:
        while not stop_event.is_set():
            try:
                pod_cpu_m, pod_rss_mib, node_cpu_m, node_rss_mib = await _sample_once(
                    kube
                )
                series.append(
                    {
                        "t": time.monotonic() - start,
                        "pod_cpu_m": pod_cpu_m,
                        "pod_rss_mib": pod_rss_mib,
                        "node_cpu_m": node_cpu_m,
                        "node_rss_mib": node_rss_mib,
                    }
                )
            except Exception:  # noqa: BLE001: sampling is best-effort
                if not logged_error:
                    logger.warning(
                        "load-test resource sampling unavailable; "
                        "continuing with best-effort series",
                        exc_info=True,
                    )
                    logged_error = True
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
    finally:
        if owns_client:
            await kube.close()
    return series


async def _sample_once(
    kube: KubernetesClient,
) -> tuple[float | None, float | None, float | None, float | None]:
    """One sample: fc-invoke pod cpu/rss (from pod-metrics) + node-4 cpu/rss."""
    pod_cpu_m = pod_rss_mib = None
    pod_metrics = await kube.pod_metrics(FC_INVOKE_NAMESPACE)
    for item in pod_metrics:
        name = (item.get("metadata", {}) or {}).get("name", "")
        if name.startswith(FC_INVOKE_POD_PREFIX):
            pod_cpu_m, pod_rss_mib = _pod_cpu_milli_and_rss_mib(item)
            break

    node = await kube.aggregate_node_resources()
    node_cpu_m = node.get("cpu_used_cores", 0.0) * 1000.0
    node_rss_mib = node.get("memory_used_bytes", 0.0) / (1024**2)
    return pod_cpu_m, pod_rss_mib, node_cpu_m, node_rss_mib


async def run_load_test(
    run_id: str,
    workload: str,
    store: LoadStore,
    duration_s: int = 120,
    client_concurrency: int = 32,
    corpus: list[dict] | None = None,
    sampler: Callable[[asyncio.Event], Awaitable[list[dict]]] | None = None,
    invoke: Callable[..., Awaitable[tuple[dict, dict, str | None]]] | None = None,
) -> None:
    """Drain ``corpus`` through the daemon for ``duration_s`` seconds.

    ``client_concurrency`` (32) intentionally EXCEEDS the daemon's per-workload
    concurrency (16), so the daemon's semaphore stays saturated and the drain
    runs as fast as the daemon allows. Each of ``client_concurrency`` worker
    tasks loops until the deadline, pulling the next corpus item round-robin,
    invoking the daemon, timing wall latency, and recording the row. A resource
    sampler runs alongside and is cancelled after the workers finish; then the
    store is finalized (tail flush + summary + run marked done).

    ``sampler`` and ``invoke`` are injectable for tests (defaults use the real
    ones). No scan exception ever kills a worker: it is caught and recorded as a
    ``status='error'`` row.
    """
    spec = WORKLOADS[workload]
    if corpus is None:
        corpus = load_corpus(workload)
    if not corpus:
        await store.finalize(run_id)
        return
    invoke_fn = invoke or _invoke
    sampler_fn = sampler or sample_resources

    build_payload = spec["build_payload"]
    parse_result = spec["parse_result"]
    read_timeout = spec["read_timeout"]

    deadline = time.monotonic() + duration_s
    counter = itertools.count()
    seq_counter = itertools.count()

    stop_event = asyncio.Event()
    sampler_task = asyncio.create_task(sampler_fn(stop_event))

    async def worker() -> None:
        while time.monotonic() < deadline:
            i = next(counter)
            item = corpus[i % len(corpus)]
            seq = next(seq_counter)
            started = perf_counter()
            try:
                payload = build_payload(item)
                resp, meta, error = await invoke_fn(workload, payload, read_timeout)
                latency_ms = int((perf_counter() - started) * 1000)
                if error is not None:
                    row = {
                        "seq": seq,
                        "workload": workload,
                        "name": item["name"],
                        "status": "error",
                        "latency_ms": latency_ms,
                        "queue_wait_ms": None,
                        "cpu_ms": None,
                        "peak_rss_mib": None,
                        "result_count": None,
                        "result": None,
                        "error": error,
                    }
                else:
                    result_jsonb, result_count = parse_result(resp)
                    row = {
                        "seq": seq,
                        "workload": workload,
                        "name": item["name"],
                        "status": "ok",
                        "latency_ms": latency_ms,
                        "queue_wait_ms": meta.get("queue_wait_ms"),
                        "cpu_ms": meta.get("cpu_ms"),
                        "peak_rss_mib": meta.get("peak_rss_mib"),
                        "result_count": result_count,
                        "result": result_jsonb,
                        "error": None,
                    }
            except Exception as exc:  # noqa: BLE001
                # Never let one scan kill a worker: the failure is not swallowed
                # but recorded as a status='error' scan row (detail below), and
                # logged at debug so it is visible without flooding the log.
                logger.debug("load-test scan raised: %s", exc)
                latency_ms = int((perf_counter() - started) * 1000)
                row = {
                    "seq": seq,
                    "workload": workload,
                    "name": item.get("name"),
                    "status": "error",
                    "latency_ms": latency_ms,
                    "queue_wait_ms": None,
                    "cpu_ms": None,
                    "peak_rss_mib": None,
                    "result_count": None,
                    "result": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            await store.record(row)

    workers = [asyncio.create_task(worker()) for _ in range(client_concurrency)]
    try:
        # Workers stop starting scans at the deadline, but an in-flight invoke can
        # run up to read_timeout past it. Cap the overrun with a grace window, then
        # cancel any straggler so the run always finalizes promptly (a wedged drain
        # would otherwise hold the 'running' one-run lock and block the next run).
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers, return_exceptions=True),
                timeout=duration_s + _DRAIN_GRACE_S,
            )
        except asyncio.TimeoutError:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
    finally:
        stop_event.set()
        try:
            series = await sampler_task
        except Exception:  # noqa: BLE001: sampler is best-effort
            logger.exception("load-test resource sampler task failed")
            series = []
        store.set_sampler_series(series or [])
        await store.finalize(run_id)
