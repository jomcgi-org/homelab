"""Factory outgoing-tree validation, installed by the shim before source turns.

This is an accidental-drift guard, not a security boundary against a hostile
worker that disables Git hooks. Validation never edits the worker's checkout.
"""

import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


def git(root, *args):
    return (
        subprocess.check_output(["git", "-C", str(root), *args], env=git_env())
        .decode()
        .strip()
    )


def git_env():
    env = dict(os.environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)
    return env


def install(root, contract):
    if not isinstance(contract, dict) or set(contract) != {
        "task_id",
        "node_key",
        "branch",
        "base_ref",
    }:
        raise ValueError("invalid factory push-format contract")
    for name in ("task_id", "node_key"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", contract[name]):
            raise ValueError("invalid push-format identity")
    # The server resolves this from the admitted pin, including adoption.
    if contract["branch"] in {"main", "master"}:
        raise ValueError("push-format requires a delivery branch")
    git(root, "check-ref-format", "refs/heads/" + contract["branch"])
    if contract["base_ref"] != "refs/remotes/origin/main":
        raise ValueError("unsupported push-format baseline")
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    state = common / "factory-push-format"
    state.mkdir(exist_ok=True)
    hooks = state / "hooks"
    config = state / "contract.json"
    if not config.exists():
        previous = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "core.hooksPath"],
            capture_output=True,
            text=True,
            check=False,
        )
        if previous.returncode not in (0, 1):
            raise RuntimeError("cannot read existing hooks configuration")
        original = (
            previous.stdout.strip()
            if previous.returncode == 0
            else str(common / "hooks")
        )
        (state / "original-hooks").write_text(original)
    hooks.mkdir(exist_ok=True)
    original = (state / "original-hooks").read_text()
    original_dir = Path(original)
    if not original_dir.is_absolute():
        original_dir = Path(root) / original_dir
    # Preserve every existing hook. Relative core.hooksPath remains relative to
    # the invoking worktree, including linked worktrees.
    names = {p.name for p in original_dir.iterdir()} if original_dir.is_dir() else set()
    names.add("pre-push")
    for name in names:
        if name.endswith(".sample"):
            continue
        hook = hooks / name
        if name == "pre-push":
            body = 'exec python3 %s hook %s "$@"\n' % (
                shlex.quote(str(Path(__file__).resolve())),
                shlex.quote(str(state)),
            )
        else:
            target = shlex.quote(str(Path(original) / name))
            body = f'if [ -x {target} ]; then exec {target} "$@"; fi\n'
        hook.write_text("#!/bin/sh\n" + body)
        hook.chmod(0o755)
    temp = state / "contract.next"
    temp.write_text(json.dumps(contract))
    temp.replace(config)
    git(root, "config", "core.hooksPath", str(hooks))
    return state


def evidence(state):
    path = _evidence_path(state)
    return json.loads(path.read_text()) if path.exists() else None


def _evidence_path(state):
    state = Path(state)
    identity = json.loads((state / "contract.json").read_text())
    return state / (identity["task_id"] + "." + identity["node_key"] + ".json")


def _paths(root):
    paths = set()
    for args in (
        ("diff", "--name-only", "-z", "HEAD"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        data = subprocess.check_output(["git", "-C", str(root), *args], env=git_env())
        paths.update(os.fsdecode(p) for p in data.split(b"\0") if p)
    return sorted(paths)


def _validate(root, state, contract, oid):
    # Production hydrates --single-branch. A plain fetch of main only writes
    # FETCH_HEAD, so explicitly populate the remote-tracking baseline.
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "fetch",
            "--no-tags",
            "origin",
            "+refs/heads/main:" + contract["base_ref"],
        ],
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "cannot fetch push-validation baseline: " + result.stderr.strip()
        )
    base = git(root, "rev-parse", "--verify", contract["base_ref"] + "^{commit}")
    git(root, "merge-base", oid, base)  # Missing/shallow baseline must not pass.
    names = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "-z", base, oid], env=git_env()
    )
    if b"\n" in names or b"\r" in names:
        raise RuntimeError("ci changed-file input cannot represent newline paths")
    scratch = Path(tempfile.mkdtemp(prefix="tree-", dir=state))
    git(
        root,
        "-c",
        "core.hooksPath=/dev/null",
        "worktree",
        "add",
        "--detach",
        str(scratch),
        oid,
    )
    env = dict(os.environ, CI_BASE_REF=base, CI_REQUIRE_COMPLETE="1")
    env.pop("BUILD_WORKSPACE_DIRECTORY", None)
    env.pop("BUILD_WORKING_DIRECTORY", None)
    # Git's hook environment points to the caller's index/worktree. Never let
    # those variables redirect validation back into the worker's checkout.
    for key in git(root, "rev-parse", "--local-env-vars").splitlines():
        env.pop(key, None)
    try:
        protocol = subprocess.run(
            ["bash", "bazel/tools/ci/ci", "validation-protocol"],
            cwd=scratch,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if protocol.returncode or protocol.stdout.strip() != "factory-push-format-v1":
            raise RuntimeError(
                "outgoing ci helper lacks complete validation protocol v1; "
                "incorporate the repository CI-helper upgrade before pushing; "
                + protocol.stderr.strip()
            )
        for step in ("lint", "regen", "lint"):
            command = ["bash", "bazel/tools/ci/ci", step]
            result = subprocess.run(command, cwd=scratch, env=env, check=False)
            if result.returncode:
                raise RuntimeError(
                    f"ci {step} exited {result.returncode}; inspect {scratch}"
                )
        paths = _paths(scratch)
        if paths:
            old = evidence(state)
            if old and any(old.get(k) != contract[k] for k in ("task_id", "node_key")):
                raise RuntimeError("push-format evidence identity changed")
            paths_seen = sorted(set(paths) | set((old or {}).get("paths", [])))
            record = {
                **contract,
                "paths": paths_seen,
                "file_count": len(paths_seen),
                "outcome": "drift_blocked",
            }
            temporary = Path(state) / "evidence.next"
            temporary.write_text(json.dumps(record))
            temporary.replace(_evidence_path(state))
            print(
                json.dumps(
                    {
                        "outcome": "drift_blocked",
                        "paths": paths,
                        "outgoing_sha": oid,
                        "correction_worktree": str(scratch),
                    }
                ),
                file=sys.stderr,
            )
            raise RuntimeError(
                "outgoing tree drifted; incorporate the listed outputs, commit, then push again"
            )
        git(root, "worktree", "remove", str(scratch))
    except Exception:
        print(f"Validation worktree retained: {scratch}", file=sys.stderr)
        raise


def hook(state, remote_args, updates):
    state = Path(state)
    contract = json.loads((state / "contract.json").read_text())
    root = Path(git(".", "rev-parse", "--show-toplevel"))
    # Serialize validation and evidence updates across concurrent pushes.
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for line in updates.decode().splitlines():
            local_ref, oid, remote_ref, remote_oid = line.split()
            if remote_ref != "refs/heads/" + contract["branch"]:
                if remote_ref.startswith("refs/heads/factory/"):
                    raise RuntimeError(
                        "factory push target differs from the assigned branch"
                    )
                continue  # Ordinary human branches retain their existing hooks.
            if oid == "0" * 40:
                raise RuntimeError(
                    "source-node branch deletion is not format validation"
                )
            if not re.fullmatch(r"[0-9a-f]{40}", oid):
                raise RuntimeError("invalid outgoing commit")
            _validate(root, state, contract, oid)
        previous = Path((state / "original-hooks").read_text()) / "pre-push"
        if previous.is_file() and os.access(previous, os.X_OK):
            subprocess.run([str(previous), *remote_args], input=updates, check=True)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "install":
            print(install(sys.argv[2], json.loads(sys.argv[3])))
        elif sys.argv[1] == "hook":
            hook(sys.argv[2], sys.argv[3:], sys.stdin.buffer.read())
        else:
            raise ValueError("unknown push-format command")
    except Exception as exc:
        print(f"factory push-format refused: {exc}", file=sys.stderr)
        sys.exit(1)
