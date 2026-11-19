"""A stand-in for rclone that keeps each remote as a local directory.

Tests write this file as an executable named rclone. It implements the calls
that the mirror script makes. An object appears only when its copy completes,
as on S3. Environment:

- FAKE_REMOTES: JSON map of remote name to its root directory.
- FAKE_STATE: a directory for the call log, the upload headers and the budget.
- FAKE_BUDGET: optional. Each call and each object transfer or delete costs
  one unit. The call that runs out exits with an error at that point, so a
  test can stop a mirror run at every step.
"""

import json
import os
import shutil
import sys

args = sys.argv[1:]
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")
assert args[:3] == ["--config", "/dev/null", "--s3-no-check-bucket"], args
command, rest = args[3], args[4:]
roots = json.loads(os.environ["FAKE_REMOTES"])


def spend():
    """Use one unit of the budget, or stop as an interrupted run does."""
    if "FAKE_BUDGET" not in os.environ:
        return
    path = os.path.join(state, "budget")
    left = (
        int(open(path).read())
        if os.path.exists(path)
        else int(os.environ["FAKE_BUDGET"])
    )
    if left <= 0:
        sys.exit("interrupted")
    with open(path, "w") as out:
        out.write(str(left - 1))


def local(path):
    remote, sep, name = path.partition(":")
    assert sep and remote in roots, path
    return os.path.join(roots[remote], name)


def flag(name):
    return [rest[i + 1] for i, arg in enumerate(rest) if arg == name]


def matches(name, patterns):
    """Match rclone's root-anchored /key and /prefix/** filters."""
    for pattern in patterns:
        assert pattern.startswith("/"), pattern
        pattern = pattern[1:]
        if pattern.endswith("/**") and name.startswith(pattern[:-2]):
            return True
        if name == pattern:
            return True
    return False


def files(root):
    out = set()
    for base, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".part"):
                out.add(os.path.relpath(os.path.join(base, name), root))
    return out


def same(a, b):
    return os.path.exists(b) and open(a, "rb").read() == open(b, "rb").read()


def upload(source, target):
    spend()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, target + ".part")
    os.replace(target + ".part", target)
    path = os.path.join(state, "headers.json")
    seen = json.load(open(path)) if os.path.exists(path) else {}
    seen[target] = flag("--header-upload")
    json.dump(seen, open(path, "w"))


def remove(path):
    spend()
    os.remove(path)


spend()
if command == "lsf":
    root = local(rest[0])
    for pattern in flag("--include"):
        if os.path.isfile(os.path.join(root, pattern.lstrip("/"))):
            print(pattern.lstrip("/"))
elif command == "cat":
    sys.stdout.buffer.write(open(local(rest[0]), "rb").read())
elif command in ("copy", "sync"):
    source, target = local(rest[0]), local(rest[1])
    excludes = flag("--exclude")
    wanted = sorted(name for name in files(source) if not matches(name, excludes))
    for name in wanted:
        if not same(os.path.join(source, name), os.path.join(target, name)):
            upload(os.path.join(source, name), os.path.join(target, name))
    # rclone sync deletes after every transfer succeeds, and never deletes a
    # destination object that a filter excludes (no --delete-excluded).
    if command == "sync" and os.path.isdir(target):
        for name in sorted(files(target) - set(wanted)):
            if not matches(name, excludes):
                remove(os.path.join(target, name))
elif command == "copyto":
    upload(rest[0] if rest[0].startswith("/") else local(rest[0]), local(rest[1]))
elif command == "delete":
    for pattern in flag("--include"):
        path = os.path.join(local(rest[0]), pattern.lstrip("/"))
        if os.path.exists(path):
            remove(path)
else:
    sys.exit("unexpected command " + command)
