"""Decide whether pr-checks runs the Grimoire journey matrix (HOME-223).

The matrix (projects/monolith/dev/grimoire/ci-runner.sh) is slow and needs
a browser, so it runs only for changes that can alter what it exercises:

- any change under the Grimoire, web, auth, frontend or dev paths, the
  Python requirements, or buildbuddy.yaml;
- a pnpm-lock.yaml change that can alter what the monolith resolves.

A lockfile change can alter the monolith only if it changes a watched
importer (`.`, anything under projects/monolith/, and every importer they
link to), changes or removes an existing `packages:`/`snapshots:` entry, or
changes any other top-level section (overrides, patches, settings). New
`packages:`/`snapshots:` entries alone cannot change an existing resolution,
so a lockfile change that only adds another project's packages is skipped.

Usage: grimoire_journey_trigger.py <base-ref>
Prints `run` or `skip` and a reason. Only an explicit `skip` skips: callers
treat any other output, or a failure, as `run`.
"""

from __future__ import annotations

import subprocess
import sys

LOCKFILE = "pnpm-lock.yaml"
TRIGGER_PATHS = (
    "projects/monolith/grimoire/",
    "projects/monolith/web/",
    "projects/monolith/auth/",
    "projects/monolith/frontend/",
    "projects/monolith/dev/grimoire/",
    "bazel/requirements/",
    "buildbuddy.yaml",
)
WATCHED_IMPORTER_PREFIXES = ("projects/monolith/",)
WATCHED_IMPORTERS = (".",)
ENTRY_SECTIONS = ("packages", "snapshots")


def _key(line: str) -> str:
    return line.strip().rstrip(":").strip("'\"")


def split_sections(text: str) -> dict[str, list[str]]:
    """Top-level keys (no indent) mapped to their lines, header included."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            current = line.split(":", 1)[0]
            sections[current] = [line]
        elif current is not None:
            sections[current].append(line)
    return sections


def split_entries(lines: list[str]) -> dict[str, list[str]]:
    """Two-space keys inside one section, each mapped to its stripped body."""
    entries: dict[str, list[str]] = {}
    current = None
    for line in lines[1:]:
        if line.startswith("  ") and not line.startswith("   ") and line.strip():
            current = _key(line)
            entries[current] = []
        elif current is not None and line.strip():
            entries[current].append(line.rstrip())
    return entries


def _links(importer: str, body: list[str]) -> set[str]:
    """Importers that `importer` links to with `link:` versions."""
    out = set()
    for line in body:
        value = line.strip()
        if value.startswith("version: link:"):
            target = value.split("link:", 1)[1].strip()
            parts = [] if importer == "." else importer.split("/")
            for part in target.split("/"):
                if part == "..":
                    parts = parts[:-1]
                elif part not in ("", "."):
                    parts.append(part)
            out.add("/".join(parts) or ".")
    return out


def watched_importers(importers: dict[str, list[str]]) -> set[str]:
    todo = [
        name
        for name in importers
        if name in WATCHED_IMPORTERS or name.startswith(WATCHED_IMPORTER_PREFIXES)
    ]
    seen: set[str] = set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(_links(name, importers.get(name, [])))
    return seen


def lockfile_decision(base: str, head: str) -> tuple[bool, str]:
    """(run, reason) for a pnpm-lock.yaml change from base to head."""
    old, new = split_sections(base), split_sections(head)
    for name in sorted(set(old) | set(new)):
        if name in ENTRY_SECTIONS or name == "importers":
            continue
        if old.get(name) != new.get(name):
            return True, f"top-level section {name!r} changed"
    old_imp = split_entries(old.get("importers", []))
    new_imp = split_entries(new.get("importers", []))
    watched = watched_importers(old_imp) | watched_importers(new_imp)
    for name in sorted(watched):
        if old_imp.get(name) != new_imp.get(name):
            return True, f"importer {name!r} changed"
    for section in ENTRY_SECTIONS:
        old_entries = split_entries(old.get(section, []))
        new_entries = split_entries(new.get(section, []))
        for name, body in old_entries.items():
            if new_entries.get(name) != body:
                return True, f"{section} entry {name!r} changed or removed"
    return False, "lockfile adds entries only; watched importers unchanged"


def decide(changed: list[str], base_lock: str | None, head_lock: str | None) -> tuple[bool, str]:
    for path in changed:
        if path != LOCKFILE and path.startswith(TRIGGER_PATHS):
            return True, f"{path} changed"
    if LOCKFILE not in changed:
        return False, "no Grimoire input changed"
    if base_lock is None or head_lock is None:
        return True, "pnpm-lock.yaml added or removed"
    return lockfile_decision(base_lock, head_lock)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def _show(ref: str) -> str | None:
    try:
        return _git("show", f"{ref}:{LOCKFILE}")
    except subprocess.CalledProcessError:
        return None


def main(argv: list[str]) -> int:
    try:
        base = argv[1]
        changed = _git("diff", "--name-only", base, "HEAD", "--", LOCKFILE, *TRIGGER_PATHS).split()
        run, reason = decide(
            changed,
            _show(base) if LOCKFILE in changed else None,
            _show("HEAD") if LOCKFILE in changed else None,
        )
    except Exception as error:  # noqa: BLE001 - fail open: run the matrix
        print(f"run (trigger check failed: {error})")
        return 0
    print(f"{'run' if run else 'skip'} ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
