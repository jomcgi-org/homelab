"""Measure the real control-plane and request latency of a fork template.

Runs in the gvisor-fork-latency Jobs as the ate-system/ate-client ServiceAccount,
with the fork's kubectl-ate (an image volume of the fork build). Run it only
after the enable steps of projects/factory-cluster/README.md, "gVisor sandbox
fork (staged, off)". The design it measures is in
projects/platform-factory/substrate/egress-scan/ARCHITECTURE.md, "Engine path":
a warm pool of waiting children (mode pool) against one fork for each request
(mode ondemand and mode cold).

It reaches the ate API through kubectl-ate, so every API call includes the CLI's
own cost (a process start and a TLS handshake). The cli_floor phase measures
that cost with a cheap call: subtract it from the API timings. The request goes
straight from this script to the router, so send_to_result has no CLI cost.

Phases (MODE=control stops after the second):

  floor     cli_floor_ms          a cheap API call, the cost of one CLI call
  control   create_ms, resume_ms, create_to_ready_ms, delete_ms
                                  sequential, then K at once
  pool      send_to_result_ms     one request to each RUNNING waiting child
  ondemand  create_to_ready_ms, send_to_result_ms, total_ms
                                  create and resume a child, then one request
  cold      total_ms              create a child, send the request, let the
                                  router resume it (the request is parked)

A pool child serves one request and exits, so every request goes to a new
child. connect_ms is the TCP and TLS handshake to the router, kept apart because
a client holds its connection to the router open.

Every result line starts with "LATENCY " and holds key=value pairs, so
`kubectl -n ate-system logs job/<name> | grep '^LATENCY '` reads the run.
A line with event=sample is one measurement; a line with event=stats is the
nearest-rank p50, p95 and p99 of one metric (p99 equals max below 100 samples).
The pool books every child at the template's CPU and memory limits, and a
child that served its request keeps its booking until the worker reports its
exit and the API deletes the actor. The script reads the worker's free capacity
after the seed is ready and stops with a capacity error when the largest burst
plus one child of headroom does not fit. A worker also pins the atespace of its
first seed for the life of the process, so restart the pool pod between runs
that use different atespaces (conformance, latency, the engine).
The atespace is deleted at the end, on failure and on SIGTERM, and before the
run in case an earlier one died.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ATE_BIN = Path(os.environ.get("ATE_BIN", "/cli/ko-app/kubectl-ate"))
ENDPOINT = os.environ.get("ATE_ENDPOINT", "api.ate-system.svc:443")
TOKEN_FILE = os.environ.get("ATE_TOKEN_FILE", "/var/run/ate/token")
TEMPLATE_FILE = Path(os.environ.get("TEMPLATE_FILE", "/config/template.yaml"))
ATESPACE = os.environ.get("ATESPACE", "fork-latency")
TEMPLATE = os.environ.get("TEMPLATE", "engine")
POOL = os.environ.get("POOL", "gvisor-fork")
MODE = os.environ.get("MODE", "control")  # control | all
SEQUENTIAL = int(os.environ.get("SEQUENTIAL", "20"))
CONCURRENCY = [int(k) for k in os.environ.get("CONCURRENCY", "2,4").split(",") if k]
# Waiting children prepared at once in the sequential pool phase.
POOL_BATCH = int(os.environ.get("POOL_BATCH", "4"))
BODY_BYTES = [int(b) for b in os.environ.get("BODY_BYTES", "16384,524288").split(",")]
LARGE_SAMPLES = int(os.environ.get("LARGE_SAMPLES", "10"))
FLOOR_SAMPLES = int(os.environ.get("FLOOR_SAMPLES", "10"))
ROUTER_URL = os.environ.get("ROUTER_URL", "https://atenet-router.ate-system.svc:443")
ROUTER_CERT = os.environ.get(
    "ROUTER_CERT_FILE", "/var/run/router/credential-bundle.pem"
)
ROUTER_CA = os.environ.get("ROUTER_CA_FILE", "/var/run/router-ca/trust-bundle.pem")
REQUEST_PATH = os.environ.get("REQUEST_PATH", "/scan")
WORKER_WAIT = float(os.environ.get("WORKER_WAIT_SECONDS", "900"))
SEED_WAIT = float(os.environ.get("SEED_WAIT_SECONDS", "600"))
STATE_WAIT = float(os.environ.get("STATE_WAIT_SECONDS", "120"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30"))
POLL = float(os.environ.get("POLL_SECONDS", "0.25"))
CALL_TIMEOUT = float(os.environ.get("CALL_TIMEOUT_SECONDS", "300"))
RAW = os.environ.get("RAW", "1") == "1"
IMAGE_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})\b")
# The AWS documentation's example key: not a credential.
FAKE_SECRET = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"

_print_lock = threading.Lock()


def log(**fields: object) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"ts={stamp}"] + [f"{k}={v}" for k, v in fields.items()]
    with _print_lock:
        print("LATENCY " + " ".join(parts), flush=True)


class Failed(Exception):
    pass


def ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0


def ate(*args: str) -> subprocess.CompletedProcess:
    # --preview sends the Ate-Preview-Allowed header. The API refuses to create
    # an Actor of a fork template without it.
    cmd = [
        str(ATE_BIN),
        "--endpoint",
        ENDPOINT,
        "--token-file",
        TOKEN_FILE,
        "--preview",
        *args,
    ]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired as err:
        raise Failed(f"{' '.join(args)}: no answer in {CALL_TIMEOUT}s") from err
    except OSError as err:
        raise Failed(f"{ATE_BIN}: {err}") from err


def ate_ok(*args: str) -> subprocess.CompletedProcess:
    done = ate(*args)
    if done.returncode != 0:
        log(event="call_failed", args=repr(" ".join(args)), code=done.returncode)
        for line in (done.stderr or done.stdout).strip().splitlines()[-8:]:
            log(event="call_output", line=repr(line))
        raise Failed(f"{' '.join(args)}: exit {done.returncode}")
    return done


def timed(*args: str) -> float:
    """Run an API call that must succeed; return its duration in ms."""
    start = time.monotonic()
    ate_ok(*args)
    return ms(start)


def walk(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def first(node: object, key: str) -> object:
    for item in walk(node):
        if key in item:
            return item[key]
    return None


def get_json(*args: str) -> object:
    done = ate_ok(*args, "-o", "json")
    try:
        return json.loads(done.stdout)
    except ValueError as err:
        raise Failed(f"{' '.join(args)}: not JSON: {done.stdout[:200]!r}") from err


def pool_workers() -> list[dict]:
    data = get_json("get", "workers", "--sandbox-class", "gvisor")
    return [
        w for w in walk(data) if POOL in (w.get("workerPool"), w.get("worker_pool"))
    ]


def seeded(uid: str) -> bool:
    for worker in pool_workers():
        for seed in first(worker, "seeds") or []:
            actor = seed.get("seedActor") or {}
            if actor.get("atespace") == ATESPACE and seed.get("templateUid") == uid:
                return True
    return False


def actors() -> list[dict]:
    data = get_json("get", "actors", "-a", ATESPACE)
    return [
        a
        for a in walk(data)
        if isinstance(a.get("metadata"), dict)
        and isinstance(a.get("status"), dict)
        and isinstance(a["status"].get("state"), str)
    ]


def is_seed(actor: dict) -> bool:
    return (actor["status"].get("fork") or {}).get("role") == "FORK_ROLE_SEED"


def children() -> list[dict]:
    return [a for a in actors() if not is_seed(a)]


def wait_for(what: str, limit: float, ready, detail=None) -> float:
    start = time.monotonic()
    next_note = start + 60
    while True:
        if ready():
            return time.monotonic() - start
        now = time.monotonic()
        if now - start > limit:
            raise Failed(f"{what}: not ready after {limit:.0f}s")
        if now >= next_note:
            next_note = now + 60
            log(
                event="waiting",
                what=repr(what),
                elapsed=f"{now - start:.0f}",
                seen=repr(detail() if detail else ""),
            )
        time.sleep(POLL)


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest rank: the smallest value with at least q of the samples at or below it."""
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def stats(
    phase: str, mode: str, k: int, metric: str, values: list[float], **extra
) -> None:
    if not values:
        return
    ordered = sorted(values)
    log(
        event="stats",
        phase=phase,
        mode=mode,
        k=k,
        metric=metric,
        n=len(ordered),
        p50=f"{percentile(ordered, 0.50):.1f}",
        p95=f"{percentile(ordered, 0.95):.1f}",
        p99=f"{percentile(ordered, 0.99):.1f}",
        max=f"{ordered[-1]:.1f}",
        unit="ms",
        **extra,
    )


def sample(phase: str, mode: str, k: int, **fields: object) -> None:
    fields = {
        key: f"{value:.1f}" if isinstance(value, float) else value
        for key, value in fields.items()
    }
    if RAW:
        log(event="sample", phase=phase, mode=mode, k=k, **fields)


def body_of(size: int) -> bytes:
    """A text body of exactly size bytes with one fake secret in it."""
    line = b"lorem ipsum dolor sit amet consectetur adipiscing elit\n"
    out = bytearray(FAKE_SECRET.encode())
    while len(out) < size:
        out += line
    return bytes(out[:size])


class Router:
    """One request to the router for one actor, over a new connection."""

    def __init__(self) -> None:
        parts = urlsplit(ROUTER_URL)
        self.tls = parts.scheme == "https"
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if self.tls else 80)
        self.context = None
        if self.tls:
            self.context = ssl.create_default_context(cafile=ROUTER_CA)
            self.context.load_cert_chain(ROUTER_CERT)

    def request(self, name: str, body: bytes) -> tuple[float, float, int]:
        """Return connect_ms, send_to_result_ms and the findings count."""
        conn = (
            http.client.HTTPSConnection(
                self.host, self.port, timeout=REQUEST_TIMEOUT, context=self.context
            )
            if self.tls
            else http.client.HTTPConnection(
                self.host, self.port, timeout=REQUEST_TIMEOUT
            )
        )
        try:
            start = time.monotonic()
            conn.connect()
            connect_ms = ms(start)
            start = time.monotonic()
            conn.request(
                "POST",
                REQUEST_PATH,
                body=body,
                headers={
                    "ate-target-actor": f"{ATESPACE}/{name}",
                    "Content-Type": "text/plain",
                },
            )
            response = conn.getresponse()
            payload = response.read()
            elapsed = ms(start)
        except (OSError, http.client.HTTPException) as err:
            raise Failed(f"request to {name}: {err}") from err
        finally:
            conn.close()
        if response.status != 200:
            raise Failed(f"request to {name}: HTTP {response.status}")
        # The engine prints progress dots before its JSON.
        start_json = payload.find(b"{")
        try:
            result = json.loads(payload[start_json:]) if start_json >= 0 else None
        except ValueError:
            result = None
        if not isinstance(result, dict) or "results" not in result:
            raise Failed(f"request to {name}: the body is not engine JSON")
        return connect_ms, elapsed, len(result["results"])


def create(name: str) -> float:
    return timed("create", "actor", name, "-a", ATESPACE, "--template", TEMPLATE)


def resume(name: str) -> float:
    return timed("resume", "actor", name, "-a", ATESPACE)


def delete(name: str) -> float:
    return timed("delete", "actor", name, "-a", ATESPACE)


def counter(prefix: str):
    state = {"n": 0}
    lock = threading.Lock()

    def next_name() -> str:
        with lock:
            state["n"] += 1
            return f"{prefix}-{state['n']}"

    return next_name


NAMES = counter("lat")


def prepare(name: str) -> dict:
    """Create and resume one child: it is then RUNNING and waits for a request."""
    created = create(name)
    resumed = resume(name)
    return {"name": name, "create_ms": created, "resume_ms": resumed}


def run_k(k: int, work, items: list) -> tuple[list, float]:
    """Run work(item) for every item with k at once; return results and wall ms."""
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=k) as pool:
        results = list(pool.map(work, items))
    return results, ms(start)


FORK_OVERHEAD = 16 << 20  # booked on top of a child's memory limit


def child_booking(template_text: str) -> tuple[int, int]:
    """CPU millicores and memory bytes one child books: its template limits."""
    found = re.search(
        r"(?m)^resources:\n  limits:\n    cpuMillicores: (\d+)\n    memoryBytes: (\d+)",
        template_text,
    )
    if not found:
        raise Failed("the template has no resources.limits to book")
    return int(found.group(1)), int(found.group(2)) + FORK_OVERHEAD


def free_capacity() -> tuple[int, int] | None:
    """Free CPU millicores and memory bytes of the pool's worker, if it reports them."""
    for worker in pool_workers():
        capacity = first(worker, "capacity")
        allocated = first(worker, "allocated") or {}
        if isinstance(capacity, dict) and "cpuMillicores" in capacity:
            return (
                int(capacity["cpuMillicores"]) - int(allocated.get("cpuMillicores", 0)),
                int(capacity.get("memoryBytes", 0))
                - int(allocated.get("memoryBytes", 0)),
            )
    return None


def check_capacity(template_text: str) -> None:
    """Fail early when the pool cannot book the largest burst plus one child."""
    peak = max([*CONCURRENCY, POOL_BATCH]) + 1
    cpu, memory = child_booking(template_text)
    free = free_capacity()
    if free is None:
        log(event="capacity", result="unknown", note="the worker reports no capacity")
        return
    need = (peak * cpu, peak * memory)
    log(
        event="capacity",
        children=peak,
        need_cpu_m=need[0],
        free_cpu_m=free[0],
        need_mem_mib=need[1] >> 20,
        free_mem_mib=free[1] >> 20,
    )
    if free[0] < need[0] or free[1] < need[1]:
        raise Failed(
            f"capacity: {peak} children book {need[0]}m CPU and {need[1] >> 20} MiB, "
            f"and the pool has {free[0]}m and {free[1] >> 20} MiB free: "
            "resize the pool (kvm-pool.yaml) or lower CONCURRENCY and POOL_BATCH"
        )


def phase_floor() -> None:
    values = []
    for _ in range(FLOOR_SAMPLES):
        start = time.monotonic()
        ate_ok("get", "actors", "-a", ATESPACE, "-o", "json")
        values.append(ms(start))
    stats("floor", "api", 1, "cli_floor_ms", values)


def control_stats(phase: str, k: int, rows: list[dict], deletes: list[float]) -> None:
    for metric in ("create_ms", "resume_ms"):
        stats(phase, "control", k, metric, [r[metric] for r in rows])
    stats(
        phase,
        "control",
        k,
        "create_to_ready_ms",
        [r["create_ms"] + r["resume_ms"] for r in rows],
    )
    stats(phase, "control", k, "delete_ms", deletes)


def phase_control() -> None:
    rows, deletes = [], []
    for _ in range(SEQUENTIAL):
        row = prepare(NAMES())
        rows.append(row)
        sample(
            "control",
            "control",
            1,
            create_ms=f"{row['create_ms']:.1f}",
            resume_ms=f"{row['resume_ms']:.1f}",
        )
        deletes.append(delete(row["name"]))
    control_stats("control_seq", 1, rows, deletes)
    for k in CONCURRENCY:
        rows, wall = run_k(k, prepare, [NAMES() for _ in range(k)])
        deletes, _ = run_k(k, delete, [r["name"] for r in rows])
        control_stats(f"control_k{k}", k, rows, deletes)
        log(
            event="burst",
            k=k,
            wall_ms=f"{wall:.1f}",
            per_second=f"{k / wall * 1000:.2f}",
        )


def drained(timeout: float) -> float:
    """Wait until no child is left. Return ms from now until the last one went."""
    start = time.monotonic()
    wait_for("the children gone", timeout, lambda: not children())
    return ms(start)


def send(router: Router, name: str, size: int) -> dict:
    connect_ms, result_ms, findings = router.request(name, body_of(size))
    return {
        "connect_ms": connect_ms,
        "send_to_result_ms": result_ms,
        "findings": findings,
    }


def phase_pool(router: Router) -> None:
    for size in BODY_BYTES:
        count = SEQUENTIAL if size <= 65536 else LARGE_SAMPLES
        names = [NAMES() for _ in range(count)]
        rows = []
        for start in range(0, count, POOL_BATCH):
            batch = names[start : start + POOL_BATCH]
            run_k(len(batch), prepare, batch)
            for name in batch:
                row = send(router, name, size)
                rows.append(row)
                sample("pool", "pool", 1, body=size, **row)
            # A served child keeps its booking until its actor is deleted.
            drained(STATE_WAIT)
        stats(
            "pool_seq",
            "pool",
            1,
            "send_to_result_ms",
            [r["send_to_result_ms"] for r in rows],
            body=size,
        )
        stats(
            "pool_seq",
            "pool",
            1,
            "connect_ms",
            [r["connect_ms"] for r in rows],
            body=size,
        )
        log(
            event="drain",
            phase="pool_seq",
            body=size,
            wait_ms=f"{drained(STATE_WAIT):.1f}",
        )
    size = BODY_BYTES[0]
    for k in CONCURRENCY:
        names = [NAMES() for _ in range(k)]
        run_k(k, prepare, names)
        rows, wall = run_k(k, lambda n: send(router, n, size), names)
        stats(
            f"pool_k{k}",
            "pool",
            k,
            "send_to_result_ms",
            [r["send_to_result_ms"] for r in rows],
            body=size,
        )
        log(event="burst", phase=f"pool_k{k}", k=k, wall_ms=f"{wall:.1f}")
        log(event="drain", phase=f"pool_k{k}", wait_ms=f"{drained(STATE_WAIT):.1f}")


def on_demand(router: Router, size: int, name: str) -> dict:
    row = prepare(name)
    row.update(send(router, name, size))
    row["create_to_ready_ms"] = row["create_ms"] + row["resume_ms"]
    row["total_ms"] = row["create_to_ready_ms"] + row["send_to_result_ms"]
    return row


def cold(router: Router, size: int, name: str) -> dict:
    """Create a child and send at once: the router resumes it with the request parked."""
    start = time.monotonic()
    created = create(name)
    row = send(router, name, size)
    row["create_ms"] = created
    row["total_ms"] = ms(start)
    return row


def phase_requests(router: Router, phase: str, work) -> None:
    size = BODY_BYTES[0]
    for k, count in [(1, SEQUENTIAL)] + [(k, k) for k in CONCURRENCY]:
        names = [NAMES() for _ in range(count)]
        if k == 1:
            rows = []
            for name in names:
                row = work(router, size, name)
                rows.append(row)
                sample(phase, phase, 1, **row)
        else:
            rows, _ = run_k(k, lambda n: work(router, size, n), names)
        for metric in (
            "create_ms",
            "create_to_ready_ms",
            "send_to_result_ms",
            "total_ms",
        ):
            stats(
                f"{phase}_k{k}",
                phase,
                k,
                metric,
                [r[metric] for r in rows if metric in r],
                body=size,
            )
        drained(STATE_WAIT)


def cleanup() -> None:
    """Delete what a run leaves: the children, the template, then the atespace."""
    listed = ate("get", "actors", "-a", ATESPACE, "-o", "json")
    if listed.returncode != 0:
        log(event="cleanup", atespace=ATESPACE, code=listed.returncode, note="absent")
        return
    try:
        for actor in children():
            ate("delete", "actor", str(actor["metadata"]["name"]), "-a", ATESPACE)
        ate("delete", "actor-template", TEMPLATE, "-a", ATESPACE)
        wait_for(
            "the seed removed",
            STATE_WAIT,
            lambda: not [a for a in actors() if is_seed(a)],
        )
    except Failed as err:
        log(event="cleanup_wait", message=repr(str(err)))
    done = ate("delete", "atespace", ATESPACE)
    log(event="cleanup", atespace=ATESPACE, code=done.returncode)


def engine_image_is_pinned(text: str) -> bool:
    found = IMAGE_DIGEST.findall(text)
    return bool(found) and all(set(digest) != {"0"} for digest in found)


def run() -> None:
    cleanup()
    waited = wait_for(
        f"an ACTIVE {POOL} worker",
        WORKER_WAIT,
        lambda: any(
            str(first(w, "state")) == "WORKER_STATE_ACTIVE" for w in pool_workers()
        ),
    )
    log(event="worker", seconds=f"{waited:.2f}")
    ate_ok("create", "atespace", ATESPACE)
    ate_ok("create", "actor-template", "-f", str(TEMPLATE_FILE))
    data = get_json("get", "actor-template", TEMPLATE, "-a", ATESPACE)
    uid = first(first(data, "metadata") or {}, "uid")
    if not uid:
        raise Failed(f"actor-template {TEMPLATE}: no metadata.uid")
    seconds = wait_for("a ready seed", SEED_WAIT, lambda: seeded(str(uid)))
    log(event="seed", seed_ready_seconds=f"{seconds:.2f}")
    check_capacity(TEMPLATE_FILE.read_text())
    phase_floor()
    phase_control()
    if MODE != "all":
        log(event="data_phase", result="skipped", reason="MODE=control")
        return
    router = Router()
    phase_pool(router)
    phase_requests(router, "ondemand", on_demand)
    phase_requests(router, "cold", cold)


def main() -> int:
    log(
        event="start",
        endpoint=ENDPOINT,
        atespace=ATESPACE,
        pool=POOL,
        mode=MODE,
        sequential=SEQUENTIAL,
        concurrency=",".join(map(str, CONCURRENCY)),
    )
    text = TEMPLATE_FILE.read_text()
    if not engine_image_is_pinned(text):
        log(
            event="error",
            message=repr(
                "the template's image has no real sha256 digest (placeholder?)"
            ),
        )
        log(event="summary", result="fail")
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    status = "fail"
    try:
        run()
        status = "ok"
    except Failed as err:
        log(event="error", message=repr(str(err)))
    except Exception as err:  # the log must end in a summary line
        log(event="error", message=repr(f"{type(err).__name__}: {err}"))
    finally:
        try:
            cleanup()
        except Failed as err:
            log(event="cleanup_error", message=repr(str(err)))
        log(event="summary", result=status)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
