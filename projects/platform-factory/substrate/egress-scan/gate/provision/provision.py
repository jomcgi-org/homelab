"""Provision (or tear down) the atespace of the egress scanner's engine.

Runs in the egress-engine-provision Job (or the teardown Job) as the
ate-system/ate-client ServiceAccount, with the fork's kubectl-ate (an image
volume of the fork build), as the conformance Job does. ate-client is a global
owner of the ate API (ateApi.authorization.bootstrapOwners in
../../values/substrate.yaml). Creating an atespace and a fork template needs
that: an editor cannot create a fork template (can_create_fork_template).

MODE=apply (the default) makes three objects, each only if it is not there:

  1. the atespace          egress-scan-engine
  2. its access policy     editor: user:system:serviceaccount:ate-system:engine-gate
  3. the fork template     engine   (template.yaml: engine-pro, by digest)

then waits for a ready seed of the template on the pool's worker. Run it again
at any time: an object that exists is left alone, an access policy that lacks
the editor member gets it added (the other bindings stay), and a template that
exists with another image is an error, because a template is replaced
blue/green (a new name, then the gate flag), never edited.

MODE=teardown removes everything in the reverse order: the actors of the
atespace (the gate's children; stop the gate first), the templates (which
removes the seeds), the wait for the seeds to go, the access policy, the
atespace. It deletes the atespace only after it is empty, as the API demands.

Every result line starts with "PROVISION " and holds key=value pairs, so
`kubectl -n ate-system logs job/<name> | grep '^PROVISION '` reads the run. A
line never holds the template text or a token.
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
TEMPLATE_JSON_FILE = Path(os.environ.get("TEMPLATE_JSON_FILE", "/config/template.json"))
# Fixed in code, never read from the environment: the Job names the objects it
# creates and the principal that it makes an editor, so a stray variable must
# not move them. engine-gate takes the same names by flag (../README.md).
ATESPACE = "egress-scan-engine"
# The template name comes from template.yaml (apply sets it); "engine" is the
# name of the one that engine-gate uses.
TEMPLATE = "engine"
POOL = os.environ.get("POOL", "gvisor-fork")
# The editor member. The API wants "user:<principal>". The principal of a
# ServiceAccount token is system:serviceaccount:<ns>:<name>.
EDITOR = "user:system:serviceaccount:ate-system:engine-gate"
MODE = os.environ.get("MODE", "apply")
WORKER_WAIT = float(os.environ.get("WORKER_WAIT_SECONDS", "900"))
SEED_WAIT = float(os.environ.get("SEED_WAIT_SECONDS", "600"))
STATE_WAIT = float(os.environ.get("STATE_WAIT_SECONDS", "120"))
POLL = float(os.environ.get("POLL_SECONDS", "3"))
CALL_TIMEOUT = float(os.environ.get("CALL_TIMEOUT_SECONDS", "300"))
IMAGE_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})\b")


def log(**fields: object) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"ts={stamp}"] + [f"{k}={v}" for k, v in fields.items()]
    print("PROVISION " + " ".join(parts), flush=True)


class Failed(Exception):
    pass


def ate(*args: str) -> subprocess.CompletedProcess:
    # --preview sends the Ate-Preview-Allowed header, which fork templates need.
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


def wait_for(what: str, limit: float, ready, detail=None) -> float:
    """Poll `ready` until it is true or `limit` seconds pass.

    A call that fails inside `ready` (the API restarts, a call times out, the
    answer is not JSON) is not an answer: the poll goes on, and the last error
    is in the message if the limit passes. Only the limit ends a wait.
    """
    start = time.monotonic()
    next_note = start + 60
    last_error = ""
    while True:
        try:
            if ready():
                return time.monotonic() - start
        except Failed as err:
            last_error = str(err)
        now = time.monotonic()
        if now - start > limit:
            tail = f" (last error: {last_error})" if last_error else ""
            raise Failed(f"{what}: not ready after {limit:.0f}s{tail}")
        if now >= next_note:
            next_note = now + 60
            try:
                seen = repr(detail() if detail else "")
            except Failed as err:
                seen = repr(f"unreadable: {err}")
            log(
                event="waiting",
                what=repr(what),
                elapsed=f"{now - start:.0f}",
                seen=seen,
                last_error=repr(last_error),
            )
        time.sleep(POLL)


# --- pure helpers (tested without a cluster) -------------------------------


def image_digests(text: str) -> set[str]:
    return set(IMAGE_DIGEST.findall(text))


def template_is_pinned(text: str) -> bool:
    """The template names its engine image by a real digest, not all zeros."""
    found = image_digests(text)
    return bool(found) and all(set(d) != {"0"} for d in found)


def template_checks(text: str) -> list[str]:
    """What the template must say for the gate to work. Returns the problems."""
    problems = []
    if not template_is_pinned(text):
        problems.append("the image has no real sha256 digest")
    if not re.search(rf"(?m)^  atespace: {re.escape(ATESPACE)}$", text):
        problems.append(f"metadata.atespace is not {ATESPACE}")
    if not re.search(rf"(?m)^  name: {re.escape(TEMPLATE)}$", text):
        problems.append(f"metadata.name is not {TEMPLATE}")
    for pattern, why in (
        (r"(?m)^\s+- 0\.0\.0\.0:80$", "forksrv-net does not listen on port 80"),
        (r"(?m)^\s+port: 80$", "the wakeup probe is not on port 80"),
        (r"(?m)^\s+singleUse: true$", "singleUse is not true"),
        (r"(?m)^\s+egress: FORK_EGRESS_NONE$", "egress is not none"),
        (r"(?m)^\s+maxLifetimeSeconds: 900$", "maxLifetimeSeconds is not 900"),
        (
            r"(?m)^\s+mode: GOLDEN_SNAPSHOT_MODE_DISABLED$",
            "golden snapshots are not disabled",
        ),
    ):
        if not re.search(pattern, text):
            problems.append(why)
    if re.search(r"(?m)^\s+env:", text):
        problems.append(
            "the template names an env (the placeholder token is in the image)"
        )
    return problems


def template_json_problems(text: str, desired: dict) -> list[str]:
    """template.json (the compare copy) must agree with template.yaml (the
    create copy) on what the container cannot parse YAML to check: the image,
    the atespace and the name. provision_test.py checks that the two files are
    equal."""
    problems = []
    images = {c.get("image", "") for c in desired.get("containers") or []}
    if image_digests(" ".join(images)) != image_digests(text):
        problems.append("template.json and template.yaml name different images")
    meta = desired.get("metadata") or {}
    if meta.get("atespace") != ATESPACE:
        problems.append("template.json metadata.atespace is not the atespace")
    if not re.search(rf"(?m)^  name: {re.escape(str(meta.get('name')))}$", text):
        problems.append("template.json and template.yaml name different templates")
    return problems


def policy_with_editor(current: object, member: str) -> tuple[dict, bool]:
    """The policy manifest that holds `member` as an editor, and whether it changed.

    `current` is the JSON of `get access-policy` (or None for no policy). The
    other bindings and members stay. An update needs metadata.uid and
    metadata.version of the stored policy.
    """
    meta = (first(current, "metadata") or {}) if current else {}
    bindings = []
    for b in (first(current, "bindings") or []) if current else []:
        if isinstance(b, dict) and "role" in b:
            bindings.append(
                {"role": b["role"], "members": list(b.get("members") or [])}
            )
    changed = True
    for b in bindings:
        if b["role"] == "editor":
            if member in b["members"]:
                changed = False
            else:
                b["members"].append(member)
            break
    else:
        bindings.append({"role": "editor", "members": [member]})
    manifest: dict = {"bindings": bindings}
    if current and meta.get("uid"):
        manifest["metadata"] = {"uid": meta["uid"], "version": meta.get("version", "0")}
    return manifest, changed


def template_diff(existing: object, desired: object, path: str = "") -> list[str]:
    """Paths where the stored template differs from the wanted one.

    The stored template has more than the wanted one (uid, version, times,
    status, defaults the API fills in), so every value of `desired` must be in
    `existing` at the same path, and nothing else is compared. A number and its
    decimal string are equal (protojson prints an int64 as a string). A list is
    compared item by item and must have the same length.
    """
    here = path or "."
    if isinstance(desired, dict):
        if not isinstance(existing, dict):
            return [here]
        out: list[str] = []
        for key, want in desired.items():
            if key not in existing:
                out.append(f"{path}.{key}")
            else:
                out += template_diff(existing[key], want, f"{path}.{key}")
        return out
    if isinstance(desired, list):
        if not isinstance(existing, list) or len(existing) != len(desired):
            return [here]
        out = []
        for i, (have, want) in enumerate(zip(existing, desired)):
            out += template_diff(have, want, f"{path}[{i}]")
        return out
    return [] if str(existing) == str(desired) else [here]


def template_matches(existing: object, desired: object) -> bool:
    return not template_diff(existing, desired)


# --- the cluster steps ------------------------------------------------------


def atespace_exists() -> bool:
    return ate("get", "atespace", ATESPACE, "-o", "json").returncode == 0


def policy_json() -> object | None:
    done = ate("get", "access-policy", "-a", ATESPACE, "-o", "json")
    if done.returncode != 0 or not done.stdout.strip():
        return None  # "has no access policy" goes to stderr with exit 0
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def template_json() -> object | None:
    done = ate("get", "actor-template", TEMPLATE, "-a", ATESPACE, "-o", "json")
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


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


def seed_state() -> list[str]:
    """For the progress line: each pool worker's state and its seeds here."""
    return [
        f"{first(w, 'state')} seeds={len([x for x in first(w, 'seeds') or [] if (x.get('seedActor') or {}).get('atespace') == ATESPACE])}"
        for w in pool_workers()
    ]


def apply(workdir: Path, text: str, desired: object) -> None:
    wait_for(
        f"an ACTIVE {POOL} worker",
        WORKER_WAIT,
        lambda: (
            "WORKER_STATE_ACTIVE" in [str(first(w, "state")) for w in pool_workers()]
        ),
    )

    if atespace_exists():
        log(step="atespace", result="exists", atespace=ATESPACE)
    else:
        ate_ok("create", "atespace", ATESPACE)
        log(step="atespace", result="created", atespace=ATESPACE)

    current = policy_json()
    manifest, changed = policy_with_editor(current, EDITOR)
    if not changed:
        log(step="access_policy", result="exists", editor=EDITOR)
    else:
        path = workdir / "policy.json"
        path.write_text(json.dumps(manifest))
        verb = "update" if current else "create"
        ate_ok(verb, "access-policy", "-a", ATESPACE, "-f", str(path))
        log(step="access_policy", result=f"{verb}d", editor=EDITOR)

    existing = template_json()
    if existing is None:
        ate_ok("create", "actor-template", "-f", str(TEMPLATE_FILE))
        log(step="template", result="created", template=TEMPLATE)
    else:
        differs = template_diff(existing, desired)
        if differs:
            raise Failed(
                f"template {TEMPLATE} exists and differs at {', '.join(differs[:8])}: "
                "replace it blue/green (a new template name and the gate's "
                "--template flag), never in place"
            )
        log(step="template", result="exists", template=TEMPLATE)
    uid = str(first(first(template_json() or {}, "metadata") or {}, "uid") or "")
    if not uid:
        raise Failed(f"actor-template {TEMPLATE}: no metadata.uid")
    # The seed boots the engine and compiles its rules (about 25 s on the
    # first run) after the node pulled the private image. The wait covers both.
    seconds = wait_for("a ready seed", SEED_WAIT, lambda: seeded(uid), seed_state)
    log(step="seed", result="ready", seconds=f"{seconds:.1f}", template_uid=uid)


def actors() -> list[dict]:
    done = ate("get", "actors", "-a", ATESPACE, "-o", "json")
    if done.returncode != 0:
        # An answer that is not a list says nothing about the actors.
        raise Failed(f"get actors: exit {done.returncode}")
    return [
        a
        for a in walk(json.loads(done.stdout or "{}"))
        if isinstance(a.get("metadata"), dict)
        and isinstance(a.get("status"), dict)
        and isinstance(a["status"].get("state"), str)
    ]


def role(actor: dict) -> str:
    return str((actor["status"].get("fork") or {}).get("role") or "")


_NOT_FOUND = re.compile(r"not ?found|does not exist|no such", re.IGNORECASE)


def delete(failures: list[str], *args: str) -> None:
    """A delete that succeeds, or finds the object already gone. Any other
    failure is recorded, and the teardown fails at its end: a half removed
    atespace must not look like a clean one."""
    done = ate("delete", *args)
    if done.returncode == 0:
        return
    text = (done.stderr or done.stdout).strip()
    if _NOT_FOUND.search(text):
        return
    log(event="delete_failed", args=repr(" ".join(args)), code=done.returncode, output=repr(text[-300:]))
    failures.append(" ".join(args))


def teardown() -> None:
    if not atespace_exists():
        log(step="teardown", result="atespace absent", atespace=ATESPACE)
        return
    failures: list[str] = []
    for actor in actors():
        if role(actor) != "FORK_ROLE_SEED":
            delete(failures, "actor", str(actor["metadata"]["name"]), "-a", ATESPACE)
    done = ate("get", "actor-template", "-a", ATESPACE, "-o", "json")
    if done.returncode == 0:
        for t in walk(json.loads(done.stdout or "{}")):
            if isinstance(t.get("metadata"), dict) and "containers" in t:
                delete(failures, "actor-template", str(t["metadata"]["name"]), "-a", ATESPACE)
    try:
        wait_for(
            "the seeds removed",
            STATE_WAIT,
            lambda: not [a for a in actors() if role(a) == "FORK_ROLE_SEED"],
        )
    except Failed as err:
        failures.append(f"the seeds removed ({err})")
        log(event="teardown_wait", message=repr(str(err)))
    # The API refuses to delete the policy or the atespace while an actor or a
    # template is left, so a failure above ends here before them.
    if failures:
        raise Failed("teardown left objects: " + "; ".join(failures))
    delete(failures, "access-policy", "-a", ATESPACE)
    delete(failures, "atespace", ATESPACE)
    if failures:
        raise Failed("teardown left objects: " + "; ".join(failures))
    log(step="teardown", result="removed", atespace=ATESPACE)


def main() -> int:
    global TEMPLATE
    log(
        event="start",
        mode=MODE,
        endpoint=ENDPOINT,
        atespace=ATESPACE,
        template=TEMPLATE,
        pool=POOL,
    )
    if MODE not in ("apply", "teardown"):
        log(event="error", message=repr(f"MODE must be apply or teardown, not {MODE}"))
        return 2
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    status = "fail"
    try:
        if MODE == "apply":
            # Before any API contact: a template that cannot work makes no call.
            text = TEMPLATE_FILE.read_text()
            desired = json.loads(TEMPLATE_JSON_FILE.read_text())
            TEMPLATE = str(desired["metadata"]["name"])
            problems = template_checks(text) + template_json_problems(text, desired)
            if problems:
                for p in problems:
                    log(event="error", message=repr(p))
                log(event="summary", mode=MODE, result="fail")
                return 1
            apply(Path(os.environ.get("WORK_DIR", "/work")), text, desired)
        else:
            teardown()
        status = "ok"
    except Failed as err:
        log(event="error", message=repr(str(err)))
    log(event="summary", mode=MODE, result=status)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
