"""A stand-in for rclone that keeps each remote as a local directory.

Tests write this file as an executable named rclone. It implements the calls
that the mirror script makes, and refuses other flags. An object appears only
when its copy completes, as on S3. Environment:

- FAKE_REMOTES: JSON map of remote name to its root directory.
- FAKE_STATE: a directory for the call log, the upload headers and the step
  counter.
- FAKE_BUDGET: optional. Each call and each object transfer or delete is one
  step. The step at the budget exits with an error, so a test can stop a
  mirror run at every step.
- FAKE_ADVANCE: optional JSON {"at", "from", "to", "manifest"}. Before step
  "at", a writer copies each payload of directory "from" into directory "to",
  then the manifest, then deletes every other object of "to".
- FAKE_SNAPSHOTS: optional directory. After each object write or delete, the
  fake copies the root of the written remote into a new numbered directory.
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

# The flags that the mirror script passes to each command, and whether each
# takes a value.
FLAGS = {
    "lsf": {"--exclude": True, "--include": True, "--max-depth": True},
    "cat": {},
    "lsjson": {"--stat": False, "--metadata": False},
    "copy": {"--exclude": True, "--header-upload": True},
    "copyto": {"--ignore-times": False, "--header-upload": True},
    "delete": {"--include": True, "--files-from-raw": True},
}
FLAGS["lsf"].update({"-R": False, "--files-only": False})
assert command in FLAGS, "unexpected command " + command
paths, flags = [], {}
i = 0
while i < len(rest):
    arg = rest[i]
    if arg.startswith("-"):
        assert arg in FLAGS[command], f"{command} does not take {arg}"
        value = rest[i + 1] if FLAGS[command][arg] else True
        flags.setdefault(arg, []).append(value)
        i += 2 if FLAGS[command][arg] else 1
    else:
        paths.append(arg)
        i += 1


def flag(name):
    return flags.get(name, [])


def write_files(source, target, manifest):
    """Advance the writer: payloads, then the manifest, then deletes."""
    names = files(source)
    for name in sorted(names - {manifest}) + [manifest]:
        os.makedirs(os.path.dirname(os.path.join(target, name)), exist_ok=True)
        shutil.copyfile(os.path.join(source, name), os.path.join(target, name))
    for name in sorted(files(target) - names):
        os.remove(os.path.join(target, name))


def spend():
    """Count one step, or stop as an interrupted run does."""
    path = os.path.join(state, "steps")
    used = int(open(path).read()) if os.path.exists(path) else 0
    advance = json.loads(os.environ.get("FAKE_ADVANCE", "null"))
    if advance and used == advance["at"]:
        write_files(advance["from"], advance["to"], advance["manifest"])
        open(os.path.join(state, "advanced"), "w").close()
    if "FAKE_BUDGET" in os.environ and used >= int(os.environ["FAKE_BUDGET"]):
        sys.exit("interrupted")
    with open(path, "w") as out:
        out.write(str(used + 1))


def local(path):
    """Return the remote and the local path of remote:path."""
    remote, sep, name = path.partition(":")
    assert sep and remote in roots, path
    return remote, os.path.join(roots[remote], name)


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


def snapshot(remote):
    target = os.environ.get("FAKE_SNAPSHOTS")
    if target:
        name = f"{len(os.listdir(target)):04d}"
        shutil.copytree(roots[remote], os.path.join(target, name))


def upload(source, remote, target):
    spend()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, target + ".part")
    os.replace(target + ".part", target)
    path = os.path.join(state, "headers.json")
    seen = json.load(open(path)) if os.path.exists(path) else {}
    seen[target] = flag("--header-upload")
    json.dump(seen, open(path, "w"))
    snapshot(remote)


def remove(remote, path):
    spend()
    os.remove(path)
    snapshot(remote)


spend()
if command == "lsf":
    _, root = local(paths[0])
    assert flag("--files-only") == [True], rest
    names = files(root)
    if not flag("-R"):
        assert flag("--max-depth") == ["1"], rest
        names = {name for name in names if "/" not in name}
    if flag("--include"):
        names = {name for name in names if matches(name, flag("--include"))}
    for name in sorted(names):
        if not matches(name, flag("--exclude")):
            print(name)
elif command == "cat":
    sys.stdout.buffer.write(open(local(paths[0])[1], "rb").read())
elif command == "lsjson":
    assert flag("--stat") and flag("--metadata"), rest
    _, target = local(paths[0])
    headers = {}
    for path in [
        os.environ.get("FAKE_PRIOR_HEADERS"),
        os.path.join(state, "headers.json"),
    ]:
        if path and os.path.exists(path):
            headers.update(json.load(open(path)))
    metadata = {
        header.split(": ", 1)[0].lower(): header.split(": ", 1)[1]
        for header in headers.get(target, [])
    }
    print(json.dumps({"IsDir": not os.path.isfile(target), "Metadata": metadata}))
elif command == "copy":
    (_, source), (remote, target) = local(paths[0]), local(paths[1])
    # rclone copy never deletes. It retries a failed copy with a new listing
    # (--retries 3), so a payload that leaves the source during the copy
    # fails only the attempt that listed it.
    for attempt in range(3):
        failed = False
        for name in sorted(files(source)):
            if matches(name, flag("--exclude")):
                continue
            path = os.path.join(source, name)
            try:
                if not same(path, os.path.join(target, name)):
                    upload(path, remote, os.path.join(target, name))
            except FileNotFoundError:
                failed = True
        if not failed:
            break
    else:
        sys.exit("copy failed")
elif command == "copyto":
    source = paths[0] if paths[0].startswith("/") else local(paths[0])[1]
    if paths[1].startswith("/"):
        spend()
        shutil.copyfile(source, paths[1])
    else:
        upload(source, *local(paths[1]))
elif command == "delete":
    remote, root = local(paths[0])
    if flag("--files-from-raw"):
        # rclone refuses other filters with --files-from-raw.
        assert list(flags) == ["--files-from-raw"], rest
        names = open(flag("--files-from-raw")[0]).read().splitlines()
    else:
        names = [pattern.lstrip("/") for pattern in flag("--include")]
    for name in names:
        if os.path.exists(os.path.join(root, name)):
            remove(remote, os.path.join(root, name))
