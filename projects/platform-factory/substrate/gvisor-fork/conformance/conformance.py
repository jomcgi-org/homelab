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
IMAGE_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})\b")
# A refusal must come from the API's validation, not from a broken call.
INFRASTRUCTURE = re.compile(
    r"unavailable|unauthenticated|deadline|connection|connect:|no route|"
    r"timed? ?out|unknown command|unknown flag|no such file",
    re.IGNORECASE,
)


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


def template_uid() -> str:
    data = get_json("get", "actor-template", TEMPLATE, "-a", ATESPACE)
    uid = first(first(data, "metadata") or {}, "uid")
    if not uid:
        raise Failed(f"actor-template {TEMPLATE}: no metadata.uid")
    return str(uid)


def seeded(uid: str) -> bool:
    """A ready seed of this template, in this atespace, is listed on a worker.

    WorkerSeed is {seedActor: {atespace, name}, seedActorUid, templateUid,
    workerEpoch}; a template's UID names its generation.
    """
    for worker in pool_workers():
        for seed in first(worker, "seeds") or []:
            actor = seed.get("seedActor") or {}
            if actor.get("atespace") == ATESPACE and seed.get("templateUid") == uid:
                return True
    return False


def actors(atespace: str) -> list[dict]:
    """Actor JSON: {metadata: {name}, actorTemplate, status: {state, fork: {role}}}."""
    data = get_json("get", "actors", "-a", atespace)
    return [
        a
        for a in walk(data)
        if isinstance(a.get("metadata"), dict)
        and isinstance(a.get("status"), dict)
        and isinstance(a["status"].get("state"), str)
    ]


def name_of(item: dict) -> str:
    return str(item["metadata"]["name"])


def state_of(actor: dict) -> str:
    return actor["status"]["state"]


def role(actor: dict) -> str:
    return str((actor["status"].get("fork") or {}).get("role") or "")


def forks() -> list[dict]:
    return [a for a in actors(ATESPACE) if role(a) == "FORK_ROLE_FORK"]


def templates(atespace: str) -> list[dict]:
    data = get_json("get", "actor-template", "-a", atespace)
    return [
        t
        for t in walk(data)
        if isinstance(t.get("metadata"), dict) and "containers" in t
    ]


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


def refused(check: str, done: subprocess.CompletedProcess, what: str) -> str:
    """Fail unless the API refused the call for a reason, and return the reason."""
    if done.returncode == 0:
        raise Failed(f"{check}: the API accepted {what}")
    reason = " ".join((done.stderr or done.stdout).split())[:300]
    if not reason or INFRASTRUCTURE.search(reason):
        raise Failed(f"{check}: {what} failed, but not as a refusal: {reason!r}")
    return reason


def seed_gone(atespace: str) -> bool:
    return not [a for a in actors(atespace) if role(a) == "FORK_ROLE_SEED"]


def cleanup() -> None:
    """Delete what a run leaves, in the order the API allows.

    Fork actors first, then the templates (deleting a template deletes its
    seed), then the wait for the seed to go, then the atespace, which the API
    refuses while it holds an actor or a template.
    """
    for atespace in (ATESPACE, OTHER_ATESPACE):
        listed = ate("get", "actors", "-a", atespace, "-o", "json")
        if listed.returncode != 0:
            log(
                event="cleanup",
                atespace=atespace,
                code=listed.returncode,
                note="absent",
            )
            continue
        for actor in actors(atespace):
            if role(actor) != "FORK_ROLE_SEED":
                ate("delete", "actor", name_of(actor), "-a", atespace)
        for template in templates(atespace):
            ate("delete", "actor-template", name_of(template), "-a", atespace)
        try:
            wait_for("the seed removed", STATE_WAIT, lambda a=atespace: seed_gone(a))
        except Failed as err:
            log(event="cleanup_wait", message=repr(str(err)))
        done = ate("delete", "atespace", atespace)
        log(event="cleanup", atespace=atespace, code=done.returncode)


def engine_image_is_pinned(text: str) -> bool:
    """The template names its engine image by a real digest, not all zeros."""
    found = IMAGE_DIGEST.findall(text)
    return bool(found) and all(set(digest) != {"0"} for digest in found)


def run(workdir: Path, text: str) -> None:
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
    uid = template_uid()
    passed("C1", template=TEMPLATE, uid=uid)

    for name, body in variants(text).items():
        path = workdir / f"{name}.yaml"
        path.write_text(body)
        done = ate("create", "actor-template", "-f", str(path))
        if done.returncode == 0:
            ate(
                "delete",
                "actor-template",
                f"bad-{name.replace('_', '-')}",
                "-a",
                ATESPACE,
            )
        passed(
            "C2",
            refused=name,
            reason=repr(refused("C2", done, f"a template with {name}")),
        )

    seconds = wait_for("a ready seed", SEED_WAIT, lambda: seeded(uid))
    passed("C3", seed_ready_seconds=f"{seconds:.2f}")

    start = time.monotonic()
    names = [f"fork-{i}" for i in range(1, FORKS + 1)]
    for name in names:
        ate_ok("create", "actor", name, "-a", ATESPACE, "--template", TEMPLATE)
        ate_ok("resume", "actor", name, "-a", ATESPACE)
    wait_for(
        f"{FORKS} RUNNING forks",
        STATE_WAIT,
        lambda: sum(state_of(a) == "ACTOR_STATE_RUNNING" for a in forks()) >= FORKS,
        lambda: [state_of(a) for a in forks()],
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
    reason = refused("C5", done, "an actor of another atespace using the template")
    passed("C5", refused="cross-atespace create", reason=repr(reason))

    first_fork = names[0]
    reason = refused(
        "C6",
        ate("suspend", "actor", first_fork, "-a", ATESPACE),
        "a suspend of a forked actor",
    )
    passed("C6", refused="suspend", reason=repr(reason))
    reason = refused(
        "C6",
        ate("resume", "actor", first_fork, "-a", ATESPACE),
        "a second resume of a forked actor",
    )
    passed("C6", refused="second resume", reason=repr(reason))

    seconds = wait_for(
        "forks deleted by their lifetime",
        LIFETIME_WAIT,
        lambda: not forks(),
        lambda: [state_of(a) for a in forks()],
    )
    passed("C7", seconds=f"{seconds:.2f}")

    ate_ok("delete", "actor-template", TEMPLATE, "-a", ATESPACE)
    wait_for("the seed removed", STATE_WAIT, lambda: not seeded(uid))
    wait_for(
        "no actor left",
        STATE_WAIT,
        lambda: not actors(ATESPACE),
        lambda: [state_of(a) for a in actors(ATESPACE)],
    )
    passed("C8", left=0)


def main() -> int:
    log(event="start", endpoint=ENDPOINT, atespace=ATESPACE, pool=POOL, forks=FORKS)
    # Before any API contact: a template that names no real engine image makes
    # no call, and leaves nothing to clean up.
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
    work = Path(os.environ.get("WORK_DIR", "/work"))
    try:
        run(work, text)
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
