"""Validate scoped invariant records and resolve live tests in the Bazel graph."""

import argparse
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as error:
    # Cached developer bundles predating HOME-347 lack PyYAML.
    if (
        error.name == "yaml"
        and __name__ == "__main__"
        and sys.argv[1:] == ["--records-only"]
    ):
        print(
            "ci lint: invariant validation deferred to required CI. "
            "Run ./bootstrap.sh to install PyYAML for local checks.",
            file=sys.stderr,
        )
        sys.exit(0)
    raise

TEMPLATE = Path("docs/templates/INVARIANTS.yaml")
LABEL = re.compile(r"//[\w./+-]*:[\w./+-]+")


class InvariantLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep=deep)
        if len(mapping) != len(node.value):
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate mapping key", node.start_mark
            )
        return mapping


def validate(documents):
    ids, live = {}, {}
    for source, document in documents.items():
        if source == TEMPLATE:
            # Validate the example schema without resolving its fictional label.
            validate_rows(document, source, {}, {})
        else:
            validate_rows(document, source, ids, live)
    return live


def validate_rows(document, source, ids, live):
    if (
        not isinstance(document, dict)
        or set(document) != {"invariants"}
        or not isinstance(document["invariants"], list)
    ):
        raise ValueError(f"{source}: expected an invariants list")
    for i, row in enumerate(document["invariants"], 1):
        where = f"{source}: row {i}"
        required = {"id", "statement", "mechanism", "state"}
        if not isinstance(row, dict) or not required <= row.keys() <= required | {
            "test"
        }:
            raise ValueError(f"{where}: invalid fields")
        ident = row["id"]
        if not isinstance(ident, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9-]*-[0-9]+", ident
        ):
            raise ValueError(f"{where}: invalid id")
        if ident in ids:
            raise ValueError(
                f"{where}: duplicate {ident}, also in {ids[ident]}. "
                "Give a new invariant the ID <issue key>-<n>, such as HOME-123-1"
            )
        ids[ident] = source
        where = f"{source}: {ident}"
        if not isinstance(row["statement"], str) or not row["statement"].strip():
            raise ValueError(f"{where}: expected a statement")
        state = row["state"]
        if state not in ("live", "designed", "unproven"):
            raise ValueError(f"{where}: unknown state {state!r}")
        paths = row["mechanism"]
        if not isinstance(paths, list) or not all(
            isinstance(p, str)
            and p.strip()
            and not Path(p).is_absolute()
            and ".." not in Path(p).parts
            for p in paths
        ):
            raise ValueError(f"{where}: mechanism must list repository paths")
        if bool(paths) != (state != "designed"):
            raise ValueError(f"{where}: mechanism must be empty exactly when designed")
        if state == "live" or "test" in row:
            label = row.get("test")
            if not isinstance(label, str) or not LABEL.fullmatch(label):
                raise ValueError(f"{where}: expected a test label")
            if state == "live":
                live.setdefault(label, []).append(where)


def eligible_tests(xml):
    return {
        rule.attrib["name"]
        for rule in ET.fromstring(xml).findall("rule")
        if rule.attrib["class"].endswith("_test")
        and rule.find("list[@name='tags']/string[@value='manual']") is None
    }


# Restrict matches to standalone comment lines so examples in strings and
# prose uses of "invariant" do not count as declarations.
MARKER = re.compile(
    rb"^\s*(?:#|//|/\*|<!--|\{\{-?\s*/\*)\s*invariant:\s*"
    rb"([A-Za-z][A-Za-z0-9-]*-[0-9]+)\s*(?:\*/(?:\s*-?\}\})?|-->)?\s*$"
)


def markers_at(revision):
    result = subprocess.run(
        ["git", "grep", "-z", "-I", "-F", "invariant:", revision, "--"],
        stdout=subprocess.PIPE,
    )
    if result.returncode == 1:
        return set()
    result.check_returncode()
    markers = set()
    for entry in result.stdout.split(b"\n"):
        if entry:
            source, line = entry.split(b"\0", 1)
            if match := MARKER.fullmatch(line):
                path = Path(os.fsdecode(source).removeprefix(revision + ":"))
                markers.add((path, match[1].decode()))
    return markers


def check_markers(documents, base):
    head = markers_at("HEAD")
    rows = {
        row["id"]: row
        for source, doc in documents.items()
        if source != TEMPLATE
        for row in doc["invariants"]
    }
    for path, ident in sorted(head):
        if ident not in rows:
            raise ValueError(f"{path}: unknown invariant {ident}")
    for ident, row in rows.items():
        for name in row["mechanism"]:
            required = Path(name)
            if not any(
                marker == ident and (path == required or required in path.parents)
                for path, marker in head
            ):
                raise ValueError(f"{name}: missing invariant marker {ident}")
    names = subprocess.check_output(
        [
            "git",
            "diff",
            "--find-renames",
            "--diff-filter=R",
            "--name-status",
            "-z",
            base,
            "HEAD",
            "--",
        ]
    ).split(b"\0")
    # Rename records contain status, old path, and new path.
    renames = {
        Path(os.fsdecode(old)): Path(os.fsdecode(new))
        for old, new in zip(names[1::3], names[2::3])
    }
    for path, ident in sorted(markers_at(base)):
        if (renames.get(path, path), ident) not in head:
            raise ValueError(f"{path}: dropped invariant marker {ident}")


def check(records_only=False, base=None):
    files = subprocess.check_output(
        [
            "git",
            "ls-files",
            "-coz",
            "--exclude-standard",
            "--",
            "INVARIANTS.yaml",
            ":(glob)**/INVARIANTS.yaml",
        ]
    )
    documents = {}
    for name in sorted(set(files.split(b"\0")) - {b""}):
        path = Path(os.fsdecode(name))
        if path.exists():
            try:
                documents[path] = yaml.load(path.read_text(), Loader=InvariantLoader)
            except yaml.YAMLError as error:
                raise ValueError(f"{path}: {error}") from error
    live = validate(documents)
    if base:
        check_markers(documents, base)
    if live and not records_only:
        expression = "set(" + " ".join(sorted(live)) + ")"
        xml = subprocess.check_output(["bazel", "query", "--output=xml", expression])
        missing = live.keys() - eligible_tests(xml)
        if missing:
            raise ValueError(
                "\n".join(
                    f"{', '.join(live[label])}: {label} must be a non-manual test"
                    for label in sorted(missing)
                )
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--records-only",
        action="store_true",
        help="validate records locally; required CI resolves live labels",
    )
    parser.add_argument(
        "--base", help="compare committed invariant markers against this revision"
    )
    args = parser.parse_args()
    if workspace := os.environ.get("BUILD_WORKSPACE_DIRECTORY"):
        os.chdir(workspace)
    try:
        check(args.records_only, args.base)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
