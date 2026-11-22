"""Smoke-test the microVM sandbox class with one actor, and time each phase.

Runs in the microvm-actor-spike Job as the ate-system/ate-client ServiceAccount
(an owner of the ate API). It drives kubectl-ate, staged in rustfs from the
v1.0.1 release by microvm-assets-init:

  wait for a microvm-spike worker -> create atespace and template
  -> golden build -> create actor -> first start -> hold -> suspend
  -> restore (resume) -> hold -> clean up

Every result line starts with "SPIKE " and holds key=value pairs, so
`kubectl -n ate-system logs job/<name> | grep '^SPIKE '` reads the run. The
`hold` lines open a window in which the worker pod's memory can be sampled from
outside (`kubectl -n substrate-microvm-spike top pod`): this Job has no right
to read pod metrics. The actor is not sent a request: that needs a router
client certificate this ServiceAccount does not have.

The atespace, template and actor are deleted at the end, on failure and on
SIGTERM, and before the run in case an earlier one died.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ATE_BIN = Path(os.environ.get("ATE_BIN", "/work/kubectl-ate"))
ASSETS_CONFIG = Path(os.environ.get("ASSETS_CONFIG", "/assets/assets.json"))
ENDPOINT = os.environ.get("ATE_ENDPOINT", "api.ate-system.svc:443")
TOKEN_FILE = os.environ.get("ATE_TOKEN_FILE", "/var/run/ate/token")
TEMPLATE_FILE = os.environ.get("TEMPLATE_FILE", "/config/template.yaml")
ATESPACE = os.environ.get("ATESPACE", "microvm-spike")
TEMPLATE = os.environ.get("TEMPLATE", "nginx")
ACTOR = os.environ.get("ACTOR", "spike-1")
POOL = os.environ.get("POOL", "microvm-spike")
WORKER_WAIT = float(os.environ.get("WORKER_WAIT_SECONDS", "1800"))
GOLDEN_WAIT = float(os.environ.get("GOLDEN_WAIT_SECONDS", "900"))
STATE_WAIT = float(os.environ.get("STATE_WAIT_SECONDS", "300"))
HOLD = float(os.environ.get("HOLD_SECONDS", "90"))
POLL = float(os.environ.get("POLL_SECONDS", "5"))
CALL_TIMEOUT = float(os.environ.get("CALL_TIMEOUT_SECONDS", "600"))


def log(**fields: object) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"ts={stamp}"] + [f"{k}={v}" for k, v in fields.items()]
    print("SPIKE " + " ".join(parts), flush=True)


class Failed(Exception):
    pass


def ate(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(ATE_BIN), "--endpoint", ENDPOINT, "--token-file", TOKEN_FILE, *args]
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired as err:
        raise Failed(f"{' '.join(args)}: no answer in {CALL_TIMEOUT}s") from err
    if check and done.returncode != 0:
        log(event="call_failed", args=repr(" ".join(args)), code=done.returncode)
        for line in (done.stderr or done.stdout).strip().splitlines()[-8:]:
            log(event="call_output", line=repr(line))
        raise Failed(f"{' '.join(args)}: exit {done.returncode}")
    return done


def walk(node: object):
    """Yield every dict in a JSON value."""
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
    done = ate(*args, "-o", "json")
    try:
        return json.loads(done.stdout)
    except ValueError as err:
        raise Failed(f"{' '.join(args)}: not JSON: {done.stdout[:200]!r}") from err


def worker_ready() -> bool:
    data = get_json("get", "workers", "--sandbox-class", "microvm")
    for item in walk(data):
        state = item.get("state")
        if (
            isinstance(state, str)
            and state == "WORKER_STATE_ACTIVE"
            and POOL in json.dumps(item)
        ):
            return True
    return False


def golden_status() -> tuple[str, str]:
    data = get_json("get", "actor-template", TEMPLATE, "-a", ATESPACE)
    tag = first(data, "goldenTag")
    name = tag.get("name", "") if isinstance(tag, dict) else ""
    return name, str(first(data, "errorMessage") or "")


def actor_state() -> str:
    data = get_json("get", "actor", ACTOR, "-a", ATESPACE)
    for item in walk(data):
        state = item.get("state")
        if isinstance(state, str) and state.startswith("ACTOR_STATE_"):
            return state
    return ""


def wait_for(what: str, limit: float, ready) -> float:
    start = time.monotonic()
    while True:
        if ready():
            return time.monotonic() - start
        if time.monotonic() - start > limit:
            raise Failed(f"{what}: not ready after {limit:.0f}s")
        time.sleep(POLL)


def timed(phase: str, *args: str) -> float:
    start = time.monotonic()
    ate(*args)
    seconds = time.monotonic() - start
    log(phase=phase, seconds=f"{seconds:.2f}", status="ok")
    return seconds


def wait_running(phase: str, seconds_so_far: float) -> float:
    """Add the wait for RUNNING to a resume that returned early."""
    waited = 0.0
    if actor_state() != "ACTOR_STATE_RUNNING":
        waited = wait_for(
            f"{phase} running",
            STATE_WAIT,
            lambda: actor_state() == "ACTOR_STATE_RUNNING",
        )
    total = seconds_so_far + waited
    log(phase=phase, seconds=f"{total:.2f}", status="running")
    return total


def hold(name: str) -> None:
    log(event="hold", name=name, seconds=f"{HOLD:.0f}", actor_state=actor_state())
    time.sleep(HOLD)


def cleanup() -> None:
    for args in (
        ("delete", "actor", ACTOR, "-a", ATESPACE),
        ("delete", "actor-template", TEMPLATE, "-a", ATESPACE),
        ("delete", "atespace", ATESPACE),
    ):
        done = ate(*args, check=False)
        log(event="cleanup", args=repr(" ".join(args)), code=done.returncode)


def check_binary() -> None:
    tool = json.loads(ASSETS_CONFIG.read_text())["tools"]["kubectl-ate"]
    digest = hashlib.sha256(ATE_BIN.read_bytes()).hexdigest()
    if digest != tool["sha256"]:
        raise Failed(f"kubectl-ate sha256 {digest}, pinned {tool['sha256']}")
    ATE_BIN.chmod(0o755)
    log(event="cli", sha256=digest)


def run() -> dict[str, float]:
    times: dict[str, float] = {}
    check_binary()
    cleanup()
    times["worker_wait"] = wait_for(f"a {POOL} worker", WORKER_WAIT, worker_ready)
    log(phase="worker_wait", seconds=f"{times['worker_wait']:.2f}", status="ok")

    ate("create", "atespace", ATESPACE)
    start = time.monotonic()
    ate("create", "actor-template", "-f", TEMPLATE_FILE)

    def golden_done() -> bool:
        tag, error = golden_status()
        if error:
            raise Failed(f"golden build failed: {error[:300]}")
        return bool(tag)

    wait_for("golden tag", GOLDEN_WAIT, golden_done)
    times["golden_build"] = time.monotonic() - start
    log(phase="golden_build", seconds=f"{times['golden_build']:.2f}", status="ok")

    times["create_actor"] = timed(
        "create_actor", "create", "actor", ACTOR, "-a", ATESPACE, "--template", TEMPLATE
    )
    times["first_start"] = wait_running(
        "first_start",
        timed("first_start_call", "resume", "actor", ACTOR, "-a", ATESPACE),
    )
    hold("idle_after_first_start")

    times["suspend"] = timed(
        "suspend", "suspend", "actor", ACTOR, "-a", ATESPACE, "--mode", "sync"
    )
    times["restore"] = wait_running(
        "restore", timed("restore_call", "resume", "actor", ACTOR, "-a", ATESPACE)
    )
    hold("idle_after_restore")
    return times


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    log(event="start", endpoint=ENDPOINT, atespace=ATESPACE, pool=POOL)
    times: dict[str, float] = {}
    status = "fail"
    try:
        times = run()
        status = "ok"
    except Failed as err:
        log(event="error", message=repr(str(err)))
    finally:
        cleanup()
        summary = {k: f"{v:.2f}" for k, v in times.items()}
        log(event="summary", result=status, **summary)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
