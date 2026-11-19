"""Hub baseline that sizes the factory AIS ingester (HOME-483).

The ``ships-baseline`` CronWorkflow runs this job for one window, 24 hours by
default, and writes one JSON object to the private R2 bucket. Every minute it
reads two sources through the Kubernetes API:

* frame counts: the ingest leader logs ``ships AIS frames minute=M count=N``
  (ships/ingest.py) before it parses a frame. The job reads these lines from
  the backend pods with ``pods/log``.
* container usage: CPU and working-set bytes of each backend container, from
  ``metrics.k8s.io``. The ingest container in a minute is the backend
  container of the pod that logged frames for that minute.

At the window end the job counts ``ships.positions`` rows and partition and
index bytes. The object is also the checkpoint. The job rewrites it every few
minutes with ``complete`` false, and a retried pod of the same workflow
resumes from it with the same window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import ssl
import statistics
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("monolith.ships.baseline")

SCHEMA = "ships-baseline/v1"
FRAME_LINE = re.compile(r"ships AIS frames minute=(\d+) count=(\d+)")
FRAMES_SOURCE = (
    "hub backend logs: 'ships AIS frames minute=M count=N' lines from "
    "ships/ingest.py, read with pods/log"
)
USAGE_SOURCE = (
    "metrics.k8s.io/v1beta1 PodMetrics: CPU cores and working-set bytes of the "
    "backend container on the pod that logged frames in the same minute"
)
POSITIONS_SOURCE = (
    "PostgreSQL pg_partition_tree('ships.positions') with count(*), "
    "pg_table_size and pg_indexes_size in one REPEATABLE READ READ ONLY "
    "transaction"
)
# The leader logs minute M when the first frame of a later minute arrives.
# Poll once more after the window ends to collect the last minute's line.
END_GRACE = timedelta(minutes=2)
# One pods/log response holds at most this many bytes. The cursor continues
# from the last complete line, so a long gap after a restart reads in parts.
LOG_LIMIT_BYTES = 16 * 1024 * 1024

POSITIONS_SQL = """
WITH counts AS (
    SELECT tableoid, count(*) AS rows FROM ships.positions GROUP BY tableoid
)
SELECT p.relid::text AS partition,
       coalesce(c.rows, 0) AS rows,
       pg_table_size(p.relid) AS partition_bytes,
       pg_indexes_size(p.relid) AS index_bytes
FROM pg_partition_tree('ships.positions') p
LEFT JOIN counts c ON c.tableoid = p.relid
WHERE p.isleaf ORDER BY p.relid::text
"""

_CPU_UNITS = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
_BYTE_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def stamp_ns(stamp: str) -> int:
    """Nanoseconds since the epoch for a kubelet RFC 3339 timestamp."""
    stamp = stamp.removesuffix("Z").removesuffix("+00:00")
    whole, _, fraction = stamp.partition(".")
    seconds = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S")
    epoch = int(seconds.replace(tzinfo=timezone.utc).timestamp())
    return epoch * 1_000_000_000 + int((fraction + "000000000")[:9])


def cpu_cores(quantity: str) -> float:
    if quantity and quantity[-1] in _CPU_UNITS:
        return float(quantity[:-1]) * _CPU_UNITS[quantity[-1]]
    return float(quantity)


def byte_count(quantity: str) -> int:
    for suffix, factor in _BYTE_UNITS.items():
        if quantity.endswith(suffix):
            return int(float(quantity[: -len(suffix)]) * factor)
    return int(float(quantity))


def frame_entries(text: str, pod: str, after_ns: int) -> tuple[list[list], int]:
    """Frame counts from one pods/log response read with timestamps.

    Returns ``[minute, pod, count]`` for each frame line newer than
    ``after_ns``, and the newest timestamp read. A response cut at
    LOG_LIMIT_BYTES ends without a newline; its last line is left for the
    next read.
    """
    lines = text.split("\n")
    lines.pop()  # the text after the last newline is incomplete or empty
    entries: list[list] = []
    newest = after_ns
    for line in lines:
        stamp, _, message = line.partition(" ")
        try:
            at = stamp_ns(stamp)
        except ValueError:
            continue
        if at <= after_ns:
            continue
        newest = max(newest, at)
        match = FRAME_LINE.search(message)
        if match:
            entries.append([int(match[1]), pod, int(match[2])])
    return entries, newest


def usage_samples(metrics: dict, container: str) -> list[list]:
    """``[unix_seconds, pod, cpu_cores, working_set_bytes]`` per pod."""
    samples = []
    for item in metrics.get("items", []):
        pod = item["metadata"]["name"]
        at = int(stamp_ns(item["timestamp"]) // 1_000_000_000)
        for entry in item.get("containers", []):
            if entry.get("name") == container:
                usage = entry.get("usage", {})
                samples.append(
                    [
                        at,
                        pod,
                        round(cpu_cores(usage.get("cpu", "0")), 6),
                        byte_count(usage.get("memory", "0")),
                    ]
                )
    return samples


def new_state(run: str, start: datetime, minutes: int) -> dict:
    start = start.replace(second=0, microsecond=0)
    return {
        "schema": SCHEMA,
        "run": run,
        "complete": False,
        "window": {
            "start": iso(start),
            "end": iso(start + timedelta(minutes=minutes)),
            "minutes": minutes,
        },
        "cursors": {},
        "series": {"frames": [], "usage": []},
    }


def window_bounds(state: dict) -> tuple[datetime, datetime]:
    window = state["window"]
    return parse_iso(window["start"]), parse_iso(window["end"])


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(len(ordered) * fraction)) - 1]


def _spread(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "p50": statistics.median(values),
        "p95": percentile(values, 0.95),
        "peak": max(values),
    }


def summarize(state: dict, positions: dict) -> dict:
    """The final object: summaries first, then the raw series they come from."""
    start, end = window_bounds(state)
    first, last = int(start.timestamp()) // 60, int(end.timestamp()) // 60
    per_minute: dict[int, int] = defaultdict(int)
    ingest_pods: dict[int, set[str]] = defaultdict(set)
    for minute, pod, count in state["series"]["frames"]:
        if first <= minute < last:
            per_minute[minute] += count
            ingest_pods[minute].add(pod)
    rates = [per_minute[m] / 60 for m in sorted(per_minute)]
    ingest = [
        sample
        for sample in state["series"]["usage"]
        if sample[1] in ingest_pods.get(sample[0] // 60, ())
    ]
    window = {"start": state["window"]["start"], "end": state["window"]["end"]}
    rate_spread = _spread(rates)
    return {
        "schema": SCHEMA,
        "run": state["run"],
        "complete": True,
        "window": state["window"],
        "messages_per_second": {
            "median": round(rate_spread["p50"], 3) if rate_spread else None,
            "p95": round(rate_spread["p95"], 3) if rate_spread else None,
            "peak": round(rate_spread["peak"], 3) if rate_spread else None,
            "minutes_observed": len(rates),
            "unit": "WebSocket frames per second, averaged over each UTC minute",
            "source": FRAMES_SOURCE,
            "window": window,
        },
        "positions": positions,
        "ingest_container": {
            "container": state.get("container", "backend"),
            "cpu_cores": _spread([sample[2] for sample in ingest]),
            "working_set_bytes": _spread([sample[3] for sample in ingest]),
            "samples": len(ingest),
            "source": USAGE_SOURCE,
            "window": window,
        },
        "cursors": state["cursors"],
        "series": state["series"],
    }


def measure_positions(engine, now: Callable[[], datetime]) -> dict:
    """Rows and bytes of ships.positions in one consistent snapshot."""
    from sqlalchemy import text

    conn = engine.connect().execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True
    )
    try:
        with conn.begin():
            observed_at = now()
            rows = conn.execute(text(POSITIONS_SQL)).all()
    finally:
        conn.close()
    partitions = [
        {
            "name": row["partition"],
            "rows": int(row["rows"]),
            "partition_bytes": int(row["partition_bytes"]),
            "index_bytes": int(row["index_bytes"]),
        }
        for row in (r._mapping for r in rows)
    ]
    return {
        "rows": sum(p["rows"] for p in partitions),
        "partition_bytes": sum(p["partition_bytes"] for p in partitions),
        "index_bytes": sum(p["index_bytes"] for p in partitions),
        "partitions": partitions,
        "source": POSITIONS_SOURCE,
        "observed_at": iso(observed_at),
    }


class KubeReader:
    """Reads backend pods, their logs and their metrics with the pod's token."""

    TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

    def __init__(self, namespace: str, selector: str, container: str) -> None:
        import httpx

        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host:
            host = f"[{host}]"
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.namespace, self.selector, self.container = namespace, selector, container
        self.http = httpx.Client(
            base_url=f"https://{host}:{port}",
            verify=ssl.create_default_context(cafile=self.CA),
            timeout=30,
        )

    def _get(self, path: str, params: dict):
        # The kubelet rotates the projected token, so read it for each call.
        with open(self.TOKEN) as handle:
            token = handle.read().strip()
        response = self.http.get(
            path, params=params, headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        return response

    def pods(self) -> list[str]:
        body = self._get(
            f"/api/v1/namespaces/{self.namespace}/pods",
            {"labelSelector": self.selector},
        ).json()
        return [item["metadata"]["name"] for item in body.get("items", [])]

    def logs(self, pod: str, since: datetime) -> str:
        return self._get(
            f"/api/v1/namespaces/{self.namespace}/pods/{pod}/log",
            {
                "container": self.container,
                "timestamps": "true",
                "sinceTime": iso(since),
                "limitBytes": str(LOG_LIMIT_BYTES),
            },
        ).text

    def metrics(self) -> dict:
        return self._get(
            f"/apis/metrics.k8s.io/v1beta1/namespaces/{self.namespace}/pods",
            {"labelSelector": self.selector},
        ).json()


class R2Object:
    """The run's object in the private bucket."""

    def __init__(self, s3, bucket: str, key: str) -> None:
        self.s3, self.bucket, self.key = s3, bucket, key

    def load(self) -> dict | None:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self.key)["Body"]
        except Exception as exc:  # botocore ClientError carries the code
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if str(code) in {"NoSuchKey", "404"}:
                return None
            raise
        try:
            return json.loads(body.read())
        finally:
            body.close()

    def save(self, value: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=json.dumps(value, separators=(",", ":")).encode(),
            ContentType="application/json",
            CacheControl="no-store",
        )


def poll(state: dict, kube, now: datetime) -> None:
    """Append new frame lines and usage samples. A failed read is logged."""
    start, _ = window_bounds(state)
    seen = {(sample[0], sample[1]) for sample in state["series"]["usage"]}
    try:
        for sample in usage_samples(kube.metrics(), kube.container):
            if (sample[0], sample[1]) not in seen:
                state["series"]["usage"].append(sample)
    except Exception:
        logger.warning("ships baseline: metrics read failed", exc_info=True)
    try:
        pods = kube.pods()
    except Exception:
        logger.warning("ships baseline: pod list failed", exc_info=True)
        return
    for pod in pods:
        cursor = state["cursors"].get(pod, 0)
        since = (
            datetime.fromtimestamp(cursor // 1_000_000_000, timezone.utc)
            if cursor
            else start
        )
        try:
            text = kube.logs(pod, since)
        except Exception:
            logger.warning("ships baseline: log read failed for %s", pod, exc_info=True)
            continue
        entries, newest = frame_entries(text, pod, cursor)
        state["series"]["frames"].extend(entries)
        state["cursors"][pod] = newest
    state["updated_at"] = iso(now)


def run(
    *,
    run_id: str,
    minutes: int,
    kube,
    store: R2Object,
    engine,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 60.0,
    save_every: timedelta = timedelta(minutes=5),
) -> dict:
    """Sample for the window, then write the complete object and return it."""
    state = store.load()
    if not state or state.get("run") != run_id:
        state = new_state(run_id, now(), minutes)
        logger.info("ships baseline: new window %s", state["window"])
    elif state.get("complete"):
        logger.info("ships baseline: run %s is already complete", run_id)
        return state
    else:
        logger.info("ships baseline: resuming window %s", state["window"])
    state["container"] = kube.container
    _, end = window_bounds(state)
    saved = now()
    store.save(state)
    while now() < end + END_GRACE:
        poll(state, kube, now())
        if now() - saved >= save_every:
            try:
                store.save(state)
                saved = now()
            except Exception:
                logger.warning("ships baseline: checkpoint failed", exc_info=True)
        sleep(interval)
    poll(state, kube, now())
    result = summarize(state, measure_positions(engine, now))
    result["written_at"] = iso(now())
    store.save(result)
    logger.info(
        "ships baseline: wrote s3://%s/%s median=%s peak=%s rows=%s",
        store.bucket,
        store.key,
        result["messages_per_second"]["median"],
        result["messages_per_second"]["peak"],
        result["positions"]["rows"],
    )
    return result


def run_from_env() -> dict:
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not endpoint.startswith("https://"):
        raise RuntimeError("SEAWEEDFS_S3_ENDPOINT must be an https R2 endpoint")
    from core.db import get_engine
    from core.s3 import path_style_client

    run_id = os.environ["SHIPS_BASELINE_RUN"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", run_id):
        raise RuntimeError("SHIPS_BASELINE_RUN must be a workflow name")
    prefix = os.environ.get("SHIPS_BASELINE_S3_PREFIX", "ships-baseline/")
    store = R2Object(
        path_style_client(endpoint),
        os.environ["SHIPS_BASELINE_S3_BUCKET"],
        f"{prefix}{run_id}.json",
    )
    kube = KubeReader(
        os.environ["SHIPS_BASELINE_NAMESPACE"],
        os.environ["SHIPS_BASELINE_POD_SELECTOR"],
        os.environ.get("SHIPS_BASELINE_CONTAINER", "backend"),
    )
    return run(
        run_id=run_id,
        minutes=int(os.environ.get("SHIPS_BASELINE_WINDOW_MINUTES", "1440")),
        kube=kube,
        store=store,
        engine=get_engine(),
    )


async def baseline_handler() -> None:
    """Sessionless job entry: the run opens its own connection at the end."""
    await asyncio.to_thread(run_from_env)
