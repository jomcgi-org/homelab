"""Run the guest-side isolation gates of the gVisor fork on the gate pool.

Runs in the gvisor-fork-gates Job as the ate-system/ate-client ServiceAccount,
with the fork's kubectl-ate (an image volume of the fork build). It uses its own
pool (workload gvisor-fork-gates), atespace (gvisor-fork-gates) and template
(template.yaml with guest.sh): nothing here touches the production pool or
engine-gate. Run it only after the pool has a worker. The log is the Job's
result: `grep '^GATES '`. The guest prints `GATE <actor> ...` lines, which the
worker logs: read them with `kubectl -n substrate-gvisor-fork-gates logs
deploy/gvisor-fork-gates | grep 'GATE '`.

Per fork, one actor named <mode>-<n>:

  ident     X4  boot_id, uuid, urandom, address-space layout and auxv, 3 forks
  canary    X7  the template's fake canary is readable; the worker's own
                environment (KUBERNETES_*, POD_UID...) and host paths are not
  net       X5  no address, no route, DNS and TCP refused (egress NONE)
  mem       X6  a fork that allocates past its limit is killed
  log       X6  a fork that writes past the log limit is killed
  pids      X6  a fork that spawns past the process limit
  hold      X1  a fork that stays 250 s, so a node-side run can ptrace it
  lifetime  X6  a fork that outlives max_lifetime_seconds is killed
  noop      X6  STORM forks, CONCURRENCY at a time, then the node-side leak check

What this script cannot do: a request through the router (X5 body) needs a router
client identity this ServiceAccount lacks, and a forged request to atelet or the
worker (X9) needs their credentials. The filter byte diff (X1) and the cgroup
and namespace leak checks run on the node.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ATE_BIN = Path(os.environ.get("ATE_BIN", "/cli/ko-app/kubectl-ate"))
ENDPOINT = os.environ.get("ATE_ENDPOINT", "api.ate-system.svc:443")
TOKEN_FILE = os.environ.get("ATE_TOKEN_FILE", "/var/run/ate/token")
TEMPLATE_FILE = Path(os.environ.get("TEMPLATE_FILE", "/config/template.yaml"))
GUEST_FILE = Path(os.environ.get("GUEST_FILE", "/config/guest.sh"))
ATESPACE = os.environ.get("ATESPACE", "gvisor-fork-gates")
TEMPLATE = os.environ.get("TEMPLATE", "gate")
POOL = os.environ.get("POOL", "gvisor-fork-gates")
STORM = int(os.environ.get("STORM", "1000"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))
MODES = os.environ.get(
    "MODES", "ident,ident,ident,canary,net,mem,log,pids,hold,lifetime"
).split(",")
WORKER_WAIT = float(os.environ.get("WORKER_WAIT_SECONDS", "900"))
SEED_WAIT = float(os.environ.get("SEED_WAIT_SECONDS", "600"))
MODE_WAIT = float(os.environ.get("MODE_WAIT_SECONDS", "120"))
LONG_WAIT = float(os.environ.get("LONG_WAIT_SECONDS", "330"))
POLL = float(os.environ.get("POLL_SECONDS", "2"))
CALL_TIMEOUT = float(os.environ.get("CALL_TIMEOUT_SECONDS", "300"))
LONG_MODES = {"hold", "lifetime"}


# gates.py acts as a global owner (ate-client), so it refuses any atespace, pool
# or template but its own: a mistaken environment must not reach the production
# pool, engine-gate's atespace or its template.
GATES_ATESPACE = "gvisor-fork-gates"
GATES_POOL = "gvisor-fork-gates"
GATES_TEMPLATE = "gate"
STORM_STOP_AFTER = 20  # consecutive failed forks that stop the storm
_consecutive_failures = 0
_failure_lock = threading.Lock()


def log(**fields: object) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"ts={stamp}"] + [f"{k}={v}" for k, v in fields.items()]
    print("GATES " + " ".join(parts), flush=True)


class Failed(Exception):
    pass


def ate(*args: str) -> subprocess.CompletedProcess:
    # --preview sends the Ate-Preview-Allowed header the API needs for a fork
    # template's actors.
    cmd = [str(ATE_BIN), "--endpoint", ENDPOINT, "--token-file", TOKEN_FILE]
    cmd += ["--preview", *args]
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
        for line in (done.stderr or done.stdout).strip().splitlines()[-6:]:
            log(event="call_output", line=repr(line))
        raise Failed(f"{' '.join(args)}: exit {done.returncode}")
    return done


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
            log(event="waiting", what=repr(what), seen=repr(detail() if detail else ""))
        time.sleep(POLL)


def pool_workers() -> list[dict]:
    data = get_json("get", "workers", "--sandbox-class", "gvisor")
    return [
        w for w in walk(data) if POOL in (w.get("workerPool"), w.get("worker_pool"))
    ]


def worker_states() -> list[str]:
    return [str(first(w, "state")) for w in pool_workers()]


def template_uid() -> str:
    data = get_json("get", "actor-template", TEMPLATE, "-a", ATESPACE)
    uid = first(first(data, "metadata") or {}, "uid")
    if not uid:
        raise Failed(f"actor-template {TEMPLATE}: no metadata.uid")
    return str(uid)


def seeded(uid: str) -> bool:
    for worker in pool_workers():
        for seed in first(worker, "seeds") or []:
            actor = seed.get("seedActor") or {}
            if actor.get("atespace") == ATESPACE and seed.get("templateUid") == uid:
                return True
    return False


def actor_state(name: str) -> str | None:
    """The actor's state, or None when the API no longer has it."""
    done = ate("get", "actor", name, "-a", ATESPACE, "-o", "json")
    if done.returncode != 0:
        return None
    try:
        return str(first(first(json.loads(done.stdout), "status") or {}, "state"))
    except ValueError:
        return None


def render_template(template: str, guest: str) -> str:
    if "__GUEST__" not in template:
        raise Failed("the template has no __GUEST__ to fill")
    return template.replace("__GUEST__", json.dumps(guest))


def run_mode(mode: str, n: int) -> dict:
    name = f"{mode}-{n}"
    start = time.monotonic()
    limit = LONG_WAIT if mode in LONG_MODES else MODE_WAIT
    created = ate("create", "actor", name, "-a", ATESPACE, "--template", TEMPLATE)
    if created.returncode != 0:
        # Record it and go on: the later modes still have results to give.
        why = " ".join((created.stderr or created.stdout).split())[-160:]
        log(event="mode", actor=name, create_rc=created.returncode, note=repr(why))
        return {"actor": name, "create_rc": created.returncode}
    resumed = ate("resume", "actor", name, "-a", ATESPACE)
    note = " ".join((resumed.stderr or resumed.stdout).split())[-160:]
    if mode == "hold" and resumed.returncode == 0:
        log(event="hold_ready", actor=name, note="node-side checks may run now")
    states: list[str] = []
    while time.monotonic() - start < limit:
        state = actor_state(name)
        label = "GONE" if state is None else state.removeprefix("ACTOR_STATE_")
        if not states or states[-1] != label:
            states.append(label)
        if state is None:
            break
        time.sleep(POLL)
    seconds = f"{time.monotonic() - start:.1f}"
    ate("delete", "actor", name, "-a", ATESPACE)
    result = {"actor": name, "resume_rc": resumed.returncode, "states": states}
    log(
        event="mode",
        actor=name,
        resume_rc=resumed.returncode,
        resume_note=repr(note if resumed.returncode else ""),
        states=">".join(states),
        seconds=seconds,
    )
    return result


def one_fork(i: int) -> bool:
    global _consecutive_failures
    with _failure_lock:
        if _consecutive_failures >= STORM_STOP_AFTER:
            return False  # the guest is broken: do not run the other forks
    name = f"noop-{i}"
    ok = False
    try:
        created = ate("create", "actor", name, "-a", ATESPACE, "--template", TEMPLATE)
        resumed = (
            ate("resume", "actor", name, "-a", ATESPACE)
            if not created.returncode
            else created
        )
        ok = resumed.returncode == 0
        return ok
    finally:
        ate("delete", "actor", name, "-a", ATESPACE)
        with _failure_lock:
            _consecutive_failures = 0 if ok else _consecutive_failures + 1


def storm() -> None:
    if STORM <= 0:
        return
    start = time.monotonic()
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for done, result in enumerate(pool.map(one_fork, range(1, STORM + 1)), 1):
            ok, failed = ok + result, failed + (not result)
            if done % 100 == 0:
                log(event="storm", done=done, ok=ok, failed=failed)
    log(
        event="storm_done",
        forks=STORM,
        ok=ok,
        failed=failed,
        seconds=f"{time.monotonic() - start:.1f}",
        note="node-side: count the forkd cgroups, network namespaces and fdh veths",
    )
    if failed:
        raise Failed(f"storm: {failed} of {STORM} forks failed")


def clean() -> None:
    listed = ate("get", "actors", "-a", ATESPACE, "-o", "json")
    if listed.returncode != 0:
        log(event="cleanup", note="no atespace")
        return
    try:
        actors = [
            a
            for a in walk(json.loads(listed.stdout))
            if isinstance(a.get("metadata"), dict) and isinstance(a.get("status"), dict)
        ]
    except ValueError:
        actors = []
    for actor in actors:
        fork = (actor["status"].get("fork") or {}).get("role")
        if fork != "FORK_ROLE_SEED":
            ate("delete", "actor", str(actor["metadata"]["name"]), "-a", ATESPACE)
    ate("delete", "actor-template", TEMPLATE, "-a", ATESPACE)
    time.sleep(5)
    done = ate("delete", "atespace", ATESPACE)
    log(event="cleanup", code=done.returncode)


def run(work: Path) -> None:
    clean()
    waited = wait_for(
        f"an ACTIVE {POOL} worker",
        WORKER_WAIT,
        lambda: "WORKER_STATE_ACTIVE" in worker_states(),
        worker_states,
    )
    log(event="worker", seconds=f"{waited:.2f}")
    text = render_template(TEMPLATE_FILE.read_text(), GUEST_FILE.read_text())
    path = work / "template.yaml"
    path.write_text(text)
    ate_ok("create", "atespace", ATESPACE)
    ate_ok("create", "actor-template", "-f", str(path))
    uid = template_uid()
    log(event="template", uid=uid, bytes=len(text))
    seconds = wait_for("a ready seed", SEED_WAIT, lambda: seeded(uid))
    log(event="seed", ready_seconds=f"{seconds:.2f}")
    counts: dict[str, int] = {}
    for mode in MODES:
        counts[mode] = counts.get(mode, 0) + 1
        run_mode(mode, counts[mode])
    storm()


def check_scope() -> None:
    if (ATESPACE, POOL, TEMPLATE) != (GATES_ATESPACE, GATES_POOL, GATES_TEMPLATE):
        raise Failed(
            f"refusing atespace {ATESPACE!r}, pool {POOL!r}, template {TEMPLATE!r}: "
            f"only {GATES_ATESPACE!r}, {GATES_POOL!r}, {GATES_TEMPLATE!r}"
        )
    if f"atespace: {GATES_ATESPACE}\n" not in TEMPLATE_FILE.read_text():
        raise Failed("the template file names another atespace")


def main() -> int:
    log(event="start", endpoint=ENDPOINT, atespace=ATESPACE, pool=POOL, storm=STORM)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    status = "fail"
    work = Path(os.environ.get("WORK_DIR", "/work"))
    in_scope = (ATESPACE, POOL, TEMPLATE) == (
        GATES_ATESPACE,
        GATES_POOL,
        GATES_TEMPLATE,
    )
    try:
        check_scope()
        run(work)
        status = "ok"
    except Failed as err:
        log(event="error", message=repr(str(err)))
    finally:
        try:
            if in_scope:
                clean()
        except Failed as err:
            log(event="cleanup_error", message=repr(str(err)))
        log(event="summary", result=status)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
