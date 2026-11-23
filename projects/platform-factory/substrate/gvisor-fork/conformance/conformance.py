"""Check a fork template end to end through the ate API.

Runs in the gvisor-fork-conformance Job as the ate-system/ate-client
ServiceAccount, with the fork's kubectl-ate (an image volume of the fork build).
Run it only after the enable steps (projects/factory-cluster/README.md,
"gVisor sandbox fork (staged, off)"). It checks the control plane and the
worker, through the API only:

  C1  a fork template is accepted                              (I7)
  C2  a template with a secret-named env, single_use false or
      egress GATEWAY is refused                                (I3, I4, I5)
  C3  the worker holds a ready seed of the template            (seed path)
  C4  N actors fork from the seed and reach RUNNING as FORK    (X10, X6)
  C5  an actor of another atespace cannot use the template     (I2, X9)
  C6  a forked actor refuses suspend and a second resume       (I4)
  C7  every forked actor is deleted by its lifetime            (I4, X6)
  C8  deleting the template removes the seed; no actor is left (X6)

It does not check what the API cannot show: a request through the router into
a child (X5) needs a router client certificate this ServiceAccount lacks, and
the child's seccomp filter, capabilities, namespaces and descriptors (X1 to X4,
X7) need the node. The README lists how those run.

Every result line starts with "CONFORMANCE " and holds key=value pairs, so
`kubectl -n ate-system logs job/<name> | grep '^CONFORMANCE '` reads the run.
The atespaces are deleted at the end, on failure and on SIGTERM, and before the
run in case an earlier one died.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ATE_BIN = Path(os.environ.get("ATE_BIN", "/cli/ko-app/kubectl-ate"))
ENDPOINT = os.environ.get("ATE_ENDPOINT", "api.ate-system.svc:443")
TOKEN_FILE = os.environ.get("ATE_TOKEN_FILE", "/var/run/ate/token")
TEMPLATE_FILE = Path(os.environ.get("TEMPLATE_FILE", "/config/template.yaml"))
ATESPACE = os.environ.get("ATESPACE", "fork-conformance")
OTHER_ATESPACE = os.environ.get("OTHER_ATESPACE", "fork-conformance-other")
TEMPLATE = os.environ.get("TEMPLATE", "engine")
POOL = os.environ.get("POOL", "gvisor-fork")
FORKS = int(os.environ.get("FORKS", "4"))
WORKER_WAIT = float(os.environ.get("WORKER_WAIT_SECONDS", "900"))
SEED_WAIT = float(os.environ.get("SEED_WAIT_SECONDS", "600"))
STATE_WAIT = float(os.environ.get("STATE_WAIT_SECONDS", "120"))
LIFETIME_WAIT = float(os.environ.get("LIFETIME_WAIT_SECONDS", "300"))
POLL = float(os.environ.get("POLL_SECONDS", "3"))
CALL_TIMEOUT = float(os.environ.get("CALL_TIMEOUT_SECONDS", "300"))
PLACEHOLDER = "@sha256:" + "0" * 64


def log(**fields: object) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"ts={stamp}"] + [f"{k}={v}" for k, v in fields.items()]
    print("CONFORMANCE " + " ".join(parts), flush=True)


class Failed(Exception):
    pass


def ate(*args: str) -> subprocess.CompletedProcess:
    cmd = [str(ATE_BIN), "--endpoint", ENDPOINT, "--token-file", TOKEN_FILE, *args]
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


def worker_states() -> list[str]:
    return [str(first(w, "state")) for w in pool_workers()]


def seeded() -> bool:
    """A ready seed of the template is listed on a worker of the pool."""
    for worker in pool_workers():
        seeds = first(worker, "seeds")
        if seeds and TEMPLATE in json.dumps(seeds):
            return True
    return False


def actors(atespace: str) -> list[dict]:
    data = get_json("get", "actors", "-a", atespace)
    return [a for a in walk(data) if isinstance(a.get("state"), str)]


def role(actor: dict) -> str:
    return str(first(actor, "role") or "")


def forks() -> list[dict]:
    return [a for a in actors(ATESPACE) if role(a) == "FORK_ROLE_FORK"]


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
            seen = repr(detail() if detail else "")
            log(
                event="waiting",
                what=repr(what),
                elapsed=f"{now - start:.0f}",
                seen=seen,
            )
        time.sleep(POLL)


def passed(check: str, **detail: object) -> None:
    log(check=check, result="pass", **detail)


def variants(text: str) -> dict[str, str]:
    """Templates that must be refused, made from the good one by one edit."""
    edits = {
        "secret_env": (
            "    wakeupProbe:",
            "    env:\n      - name: API_TOKEN\n        value: x\n    wakeupProbe:",
        ),
        "single_use_false": ("singleUse: true", "singleUse: false"),
        "egress_gateway": ("egress: FORK_EGRESS_NONE", "egress: FORK_EGRESS_GATEWAY"),
    }
    out = {}
    for name, (old, new) in edits.items():
        if old not in text:
            raise Failed(f"the template has no {old!r} to edit")
        out[name] = re.sub(
            r"(?m)^  name: .*$",
            f"  name: bad-{name.replace('_', '-')}",
            text.replace(old, new, 1),
            count=1,
        )
    return out


def create_template(path: Path) -> subprocess.CompletedProcess:
    return ate("create", "actor-template", "-f", str(path))


def cleanup() -> None:
    for atespace in (ATESPACE, OTHER_ATESPACE):
        for actor in _safe_actors(atespace):
            name = first(actor, "name")
            if name:
                ate("delete", "actor", str(name), "-a", atespace)
        done = ate("delete", "atespace", atespace)
        log(event="cleanup", atespace=atespace, code=done.returncode)


def _safe_actors(atespace: str) -> list[dict]:
    done = ate("get", "actors", "-a", atespace, "-o", "json")
    if done.returncode != 0:
        return []
    try:
        return [
            a
            for a in walk(json.loads(done.stdout))
            if "name" in a and isinstance(a.get("state"), str)
        ]
    except ValueError:
        return []


def run(workdir: Path) -> None:
    text = TEMPLATE_FILE.read_text()
    if PLACEHOLDER in text:
        raise Failed("the template's engine image is still the placeholder digest")
    cleanup()
    waited = wait_for(
        f"an ACTIVE {POOL} worker",
        WORKER_WAIT,
        lambda: "WORKER_STATE_ACTIVE" in worker_states(),
        worker_states,
    )
    log(event="worker", seconds=f"{waited:.2f}")

    ate_ok("create", "atespace", ATESPACE)
    ate_ok("create", "atespace", OTHER_ATESPACE)
    ate_ok("create", "actor-template", "-f", str(TEMPLATE_FILE))
    passed("C1", template=TEMPLATE)

    for name, body in variants(text).items():
        path = workdir / f"{name}.yaml"
        path.write_text(body)
        done = create_template(path)
        if done.returncode == 0:
            ate(
                "delete",
                "actor-template",
                f"bad-{name.replace('_', '-')}",
                "-a",
                ATESPACE,
            )
            raise Failed(f"C2: the API accepted a template with {name}")
        passed("C2", refused=name)

    seconds = wait_for("a ready seed", SEED_WAIT, seeded)
    passed("C3", seed_ready_seconds=f"{seconds:.2f}")

    start = time.monotonic()
    names = [f"fork-{i}" for i in range(1, FORKS + 1)]
    for name in names:
        ate_ok("create", "actor", name, "-a", ATESPACE, "--template", TEMPLATE)
        ate_ok("resume", "actor", name, "-a", ATESPACE)
    wait_for(
        f"{FORKS} RUNNING forks",
        STATE_WAIT,
        lambda: sum(a.get("state") == "ACTOR_STATE_RUNNING" for a in forks()) >= FORKS,
        lambda: [a.get("state") for a in forks()],
    )
    passed("C4", forks=FORKS, seconds=f"{time.monotonic() - start:.2f}")

    done = ate(
        "create",
        "actor",
        "cross-1",
        "-a",
        OTHER_ATESPACE,
        "--template",
        f"{ATESPACE}/{TEMPLATE}",
    )
    if done.returncode == 0:
        raise Failed("C5: an actor of another atespace used the template")
    passed("C5", refused="cross-atespace create")

    if ate("suspend", "actor", names[0], "-a", ATESPACE).returncode == 0:
        raise Failed("C6: a forked actor was suspended")
    if ate("resume", "actor", names[0], "-a", ATESPACE).returncode == 0:
        raise Failed("C6: a forked actor was resumed twice")
    passed("C6", refused="suspend and second resume")

    seconds = wait_for(
        "forks deleted by their lifetime",
        LIFETIME_WAIT,
        lambda: not forks(),
        lambda: [a.get("state") for a in forks()],
    )
    passed("C7", seconds=f"{seconds:.2f}")

    ate_ok("delete", "actor-template", TEMPLATE, "-a", ATESPACE)
    wait_for("the seed removed", STATE_WAIT, lambda: not seeded())
    wait_for(
        "no actor left",
        STATE_WAIT,
        lambda: not actors(ATESPACE),
        lambda: [a.get("state") for a in actors(ATESPACE)],
    )
    passed("C8", left=0)


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    log(event="start", endpoint=ENDPOINT, atespace=ATESPACE, pool=POOL, forks=FORKS)
    status = "fail"
    work = Path(os.environ.get("WORK_DIR", "/work"))
    try:
        run(work)
        status = "ok"
    except Failed as err:
        log(event="error", message=repr(str(err)))
    finally:
        try:
            cleanup()
        except Failed as err:
            log(event="cleanup_error", message=repr(str(err)))
        log(event="summary", result=status)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
