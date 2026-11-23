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
ATESPACE = os.environ.get("ATESPACE", "egress-scan-engine")
TEMPLATE = os.environ.get("TEMPLATE", "engine")
POOL = os.environ.get("POOL", "gvisor-fork")
# The members of the editor binding. The API wants "user:<principal>". The
# principal of a ServiceAccount token is system:serviceaccount:<ns>:<name>.
EDITOR = os.environ.get(
    "EDITOR_MEMBER", "user:system:serviceaccount:ate-system:engine-gate"
)
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
    if f"atespace: {ATESPACE}" not in text:
        problems.append(f"metadata.atespace is not {ATESPACE}")
    if not re.search(rf"(?m)^  name: {re.escape(TEMPLATE)}$", text):
        problems.append(f"metadata.name is not {TEMPLATE}")
    for pattern, why in (
        (r"(?m)^\s+- 0\.0\.0\.0:80$", "forksrv-net does not listen on port 80"),
        (r"(?m)^\s+port: 80$", "the wakeup probe is not on port 80"),
        (r"(?m)^\s+singleUse: true$", "singleUse is not true"),
        (r"(?m)^\s+egress: FORK_EGRESS_NONE$", "egress is not none"),
        (r"(?m)^\s+maxLifetimeSeconds: 900$", "maxLifetimeSeconds is not 900"),
    ):
        if not re.search(pattern, text):
            problems.append(why)
    if re.search(r"(?m)^\s+env:", text):
        problems.append(
            "the template names an env (the placeholder token is in the image)"
        )
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


def template_matches(existing: object, text: str) -> bool:
    """The stored template has the image digest of the wanted one."""
    return image_digests(json.dumps(existing)) == image_digests(text) and bool(
        image_digests(text)
    )


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


def apply(workdir: Path, text: str) -> None:
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
    elif template_matches(existing, text):
        log(step="template", result="exists", template=TEMPLATE)
    else:
        raise Failed(
            f"template {TEMPLATE} exists with another image: replace it blue/green "
            "(a new template name and the gate's --template flag), never in place"
        )
    uid = str(first(first(template_json() or {}, "metadata") or {}, "uid") or "")
    if not uid:
        raise Failed(f"actor-template {TEMPLATE}: no metadata.uid")
    seconds = wait_for("a ready seed", SEED_WAIT, lambda: seeded(uid))
    log(step="seed", result="ready", seconds=f"{seconds:.1f}", template_uid=uid)


def actors() -> list[dict]:
    done = ate("get", "actors", "-a", ATESPACE, "-o", "json")
    if done.returncode != 0:
        return []
    return [
        a
        for a in walk(json.loads(done.stdout or "{}"))
        if isinstance(a.get("metadata"), dict)
        and isinstance(a.get("status"), dict)
        and isinstance(a["status"].get("state"), str)
    ]


def role(actor: dict) -> str:
    return str((actor["status"].get("fork") or {}).get("role") or "")


def teardown() -> None:
    if not atespace_exists():
        log(step="teardown", result="atespace absent", atespace=ATESPACE)
        return
    for actor in actors():
        if role(actor) != "FORK_ROLE_SEED":
            ate("delete", "actor", str(actor["metadata"]["name"]), "-a", ATESPACE)
    done = ate("get", "actor-template", "-a", ATESPACE, "-o", "json")
    if done.returncode == 0:
        for t in walk(json.loads(done.stdout or "{}")):
            if isinstance(t.get("metadata"), dict) and "containers" in t:
                ate(
                    "delete",
                    "actor-template",
                    str(t["metadata"]["name"]),
                    "-a",
                    ATESPACE,
                )
    try:
        wait_for(
            "the seeds removed",
            STATE_WAIT,
            lambda: not [a for a in actors() if role(a) == "FORK_ROLE_SEED"],
        )
    except Failed as err:
        log(event="teardown_wait", message=repr(str(err)))
    log(
        step="access_policy",
        result="deleted",
        code=ate("delete", "access-policy", "-a", ATESPACE).returncode,
    )
    log(
        step="atespace",
        result="deleted",
        code=ate("delete", "atespace", ATESPACE).returncode,
    )


def main() -> int:
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
            problems = template_checks(text)
            if problems:
                for p in problems:
                    log(event="error", message=repr(p))
                log(event="summary", mode=MODE, result="fail")
                return 1
            apply(Path(os.environ.get("WORK_DIR", "/work")), text)
        else:
            teardown()
        status = "ok"
    except Failed as err:
        log(event="error", message=repr(str(err)))
    log(event="summary", mode=MODE, result=status)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
