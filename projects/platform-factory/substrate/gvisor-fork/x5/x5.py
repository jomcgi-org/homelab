"""Gate X5 for fork children: router ingress and gateway egress, on the cluster.

Runs in the gvisor-fork-x5 Job as the ate-system/ate-client ServiceAccount, with
the fork's kubectl-ate (an image volume of the fork build). It needs Substrate
fork c7f37a993862 or later (FORK_EGRESS_GATEWAY) and the ate-client SPIFFE ID in
the router's ingress allowlist (values/substrate.yaml).

  1. Create atespace fork-x5 and the probe template (template.yaml with
     probe.py), and wait for its seed.
  2. For each child: create the actor, give it policy.yaml as its own
     EgressPolicy, resume it, and send POST /run through the router. The child
     answers with its three checks (probe.py) and exits.
     mode ondemand  create, policy, resume, request, one child at a time
     mode pool      POOL_BATCH children prepared first, then one request each
  3. Pass when every child's checks pass:
     allowed  mcp-grafana answered through the gateway (any HTTP status)
     denied   the host outside the policy got no answer from its origin
     dns      the public name did not resolve

Every result line starts with "X5 " and holds key=value pairs, so
`kubectl -n ate-system logs job/<name> | grep '^X5 '` reads the run. A line
with event=stats is the nearest-rank p50 and p95 of one metric.

A worker pins the atespace of its first seed for the life of its process, so
the pool's worker is restarted before this Job (kvm-pool.yaml,
factory.jomcgi.dev/worker-restart). The atespace is deleted at the end, on
failure and on SIGTERM, and before the run in case an earlier one died.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import signal
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ATE_BIN = Path(os.environ.get("ATE_BIN", "/cli/ko-app/kubectl-ate"))
ENDPOINT = os.environ.get("ATE_ENDPOINT", "api.ate-system.svc:443")
TOKEN_FILE = os.environ.get("ATE_TOKEN_FILE", "/var/run/ate/token")
CONFIG = Path(os.environ.get("CONFIG_DIR", "/config"))
WORK = Path(os.environ.get("WORK_DIR", "/tmp"))
ATESPACE = os.environ.get("ATESPACE", "fork-x5")
TEMPLATE = os.environ.get("TEMPLATE", "probe")
POOL = os.environ.get("POOL", "gvisor-fork")
SAMPLES = int(os.environ.get("SAMPLES", "10"))
POOL_BATCH = int(os.environ.get("POOL_BATCH", "4"))
ROUTER_URL = os.environ.get("ROUTER_URL", "https://atenet-router.ate-system.svc:443")
ROUTER_CERT = os.environ.get("ROUTER_CERT_FILE", "/var/run/router/credential-bundle.pem")
ROUTER_CA = os.environ.get("ROUTER_CA_FILE", "/var/run/router-ca/trust-bundle.pem")
WORKER_WAIT = float(os.environ.get("WORKER_WAIT_SECONDS", "900"))
SEED_WAIT = float(os.environ.get("SEED_WAIT_SECONDS", "600"))
STATE_WAIT = float(os.environ.get("STATE_WAIT_SECONDS", "120"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "60"))
CALL_TIMEOUT = float(os.environ.get("CALL_TIMEOUT_SECONDS", "300"))
POLL = float(os.environ.get("POLL_SECONDS", "0.25"))


def log(**fields: object) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("X5 " + " ".join([f"ts={stamp}"] + [f"{k}={v}" for k, v in fields.items()]), flush=True)


class Failed(Exception):
    pass


def ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0


def ate(*args: str) -> subprocess.CompletedProcess:
    # --preview: the API refuses an Actor of a fork template without it.
    cmd = [str(ATE_BIN), "--endpoint", ENDPOINT, "--token-file", TOKEN_FILE, "--preview", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT, check=False)
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
    return [w for w in walk(data) if POOL in (w.get("workerPool"), w.get("worker_pool"))]


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


def wait_for(what: str, limit: float, ready) -> float:
    start = time.monotonic()
    while not ready():
        if time.monotonic() - start > limit:
            raise Failed(f"{what}: not ready after {limit:.0f}s")
        time.sleep(POLL)
    return time.monotonic() - start


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def stats(mode: str, metric: str, values: list[float]) -> None:
    if values:
        log(event="stats", mode=mode, metric=metric, n=len(values),
            p50=f"{percentile(values, 0.5):.1f}", p95=f"{percentile(values, 0.95):.1f}")


def template_text(source: str, template: str) -> str:
    """The template with probe.py's text as its command's last argument."""
    if template.count("PROBE_SOURCE") != 2:  # the comment and the argument
        raise Failed("template.yaml: PROBE_SOURCE must appear in one comment and one argument")
    head, _, tail = template.rpartition("PROBE_SOURCE")
    return head + json.dumps(source) + tail


class Router:
    def __init__(self) -> None:
        parts = urlsplit(ROUTER_URL)
        self.host, self.port = parts.hostname or "", parts.port or 443
        self.context = ssl.create_default_context(cafile=ROUTER_CA)
        self.context.load_cert_chain(ROUTER_CERT)

    def run(self, name: str) -> tuple[float, dict]:
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=REQUEST_TIMEOUT, context=self.context)
        try:
            conn.connect()
            start = time.monotonic()
            conn.request("POST", "/run", body=b"{}", headers={
                "ate-target-actor": f"{ATESPACE}/{name}", "Content-Type": "application/json"})
            response = conn.getresponse()
            payload = response.read()
            elapsed = ms(start)
        except (OSError, http.client.HTTPException) as err:
            raise Failed(f"request to {name}: {err}") from err
        finally:
            conn.close()
        if response.status != 200:
            raise Failed(f"request to {name}: HTTP {response.status} {payload[:200]!r}")
        try:
            return elapsed, json.loads(payload)
        except ValueError as err:
            raise Failed(f"request to {name}: not JSON: {payload[:200]!r}") from err


def verdict(checks: dict) -> dict[str, bool]:
    """The three X5 checks. The gateway refuses a request outside the policy with
    403, or drops the connection; mcp-grafana answers the unauthenticated MCP
    call itself (401). A 502 or 503 is the gateway failing to reach the origin,
    so it passes neither check. The sample lines keep the raw answers."""
    allowed, denied, dns = checks.get("allowed", {}), checks.get("denied", {}), checks.get("dns", {})
    return {
        "allowed": "status" in allowed and allowed["status"] not in (403, 502, 503),
        "denied": "error" in denied or denied.get("status") == 403,
        "dns": "error" in dns,
    }


def child(router: Router, name: str, prepared: bool = False) -> dict:
    row: dict = {"name": name}
    if not prepared:
        row.update(prepare(name))
    row["send_to_result_ms"], checks = router.run(name)
    row["checks"] = checks
    row["pass"] = verdict(checks)
    return row


def prepare(name: str) -> dict:
    created = timed("create", "actor", name, "-a", ATESPACE, "--template", TEMPLATE)
    policy = timed("create", "egress-policy", name, "-a", ATESPACE, "-f", str(CONFIG / "policy.yaml"))
    resumed = timed("resume", "actor", name, "-a", ATESPACE)
    return {"create_ms": created, "policy_ms": policy, "resume_ms": resumed}


def record(mode: str, rows: list[dict]) -> bool:
    ok = True
    for row in rows:
        checks, passed = row["checks"], row["pass"]
        log(event="sample", mode=mode, name=row["name"],
            send_to_result_ms=f"{row['send_to_result_ms']:.1f}",
            mcp_ms=checks.get("allowed", {}).get("ms"),
            allowed=json.dumps(checks.get("allowed"), sort_keys=True).replace(" ", ""),
            denied=json.dumps(checks.get("denied"), sort_keys=True).replace(" ", ""),
            dns=json.dumps(checks.get("dns"), sort_keys=True).replace(" ", ""),
            **{f"pass_{k}": v for k, v in passed.items()})
        ok = ok and all(passed.values())
    stats(mode, "send_to_result_ms", [r["send_to_result_ms"] for r in rows])
    stats(mode, "mcp_ms", [float(r["checks"]["allowed"]["ms"]) for r in rows if "ms" in r["checks"].get("allowed", {})])
    if mode == "ondemand":
        for key in ("create_ms", "policy_ms", "resume_ms"):
            stats(mode, key, [r[key] for r in rows])
    return ok


def cleanup() -> None:
    listed = ate("get", "actors", "-a", ATESPACE, "-o", "json")
    if listed.returncode != 0:
        log(event="cleanup", atespace=ATESPACE, note="absent")
        return
    try:
        for actor in [a for a in actors() if not is_seed(a)]:
            ate("delete", "actor", str(actor["metadata"]["name"]), "-a", ATESPACE)
        ate("delete", "actor-template", TEMPLATE, "-a", ATESPACE)
        wait_for("the seed removed", STATE_WAIT, lambda: not [a for a in actors() if is_seed(a)])
    except Failed as err:
        log(event="cleanup_wait", message=repr(str(err)))
    done = ate("delete", "atespace", ATESPACE)
    log(event="cleanup", atespace=ATESPACE, code=done.returncode)


def run() -> bool:
    cleanup()
    waited = wait_for(f"an ACTIVE {POOL} worker", WORKER_WAIT,
                      lambda: any(str(first(w, "state")) == "WORKER_STATE_ACTIVE" for w in pool_workers()))
    log(event="worker", seconds=f"{waited:.2f}")
    text = template_text((CONFIG / "probe.py").read_text(), (CONFIG / "template.yaml").read_text())
    rendered = WORK / "template.yaml"
    rendered.write_text(text)
    ate_ok("create", "atespace", ATESPACE)
    ate_ok("create", "actor-template", "-f", str(rendered))
    uid = first(first(get_json("get", "actor-template", TEMPLATE, "-a", ATESPACE), "metadata") or {}, "uid")
    if not uid:
        raise Failed(f"actor-template {TEMPLATE}: no metadata.uid")
    log(event="seed", seed_ready_seconds=f"{wait_for('a ready seed', SEED_WAIT, lambda: seeded(str(uid))):.2f}")

    router = Router()
    ok = record("ondemand", [child(router, f"od-{i}") for i in range(1, SAMPLES + 1)])
    rows: list[dict] = []
    for batch in range(0, SAMPLES, POOL_BATCH):
        names = [f"pool-{i}" for i in range(batch + 1, min(batch + POOL_BATCH, SAMPLES) + 1)]
        for name in names:
            prepare(name)
        rows += [child(router, name, prepared=True) for name in names]
    return record("pool", rows) and ok


def main() -> int:
    log(event="start", endpoint=ENDPOINT, atespace=ATESPACE, pool=POOL, samples=SAMPLES)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    status = "fail"
    try:
        status = "ok" if run() else "fail"
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
