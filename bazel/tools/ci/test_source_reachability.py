"""Fail when a tracked Python test is not owned by a runnable Bazel test.

The checker deliberately consumes Bazel's evaluated query graph. BUILD files
are Starlark, so scanning their text cannot reliably resolve macros, globs, or
source-wrapper targets. Only ``srcs`` edges count as ownership: a mention in a
comment, ``data``, or an unrelated dependency must not make a dead test look
runnable.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

TEST_QUERY = "tests(//...)"
SOURCE_GRAPH_QUERY = "tests(//...) union deps(labels(srcs, tests(//...)))"


class ReachabilityError(RuntimeError):
    """The repository's test-source ownership could not be established."""


@dataclass(frozen=True)
class QueryGraph:
    """The source ownership portion of an evaluated Bazel query graph."""

    source_labels: frozenset[str]
    src_edges: dict[str, tuple[str, ...]]


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        detail = stderr.strip() or f"exit status {result.returncode}"
        raise ReachabilityError(f"{' '.join(args)} failed: {detail}")
    return result


def repository_root(start: Path) -> Path:
    """Return the git worktree root containing ``start``."""
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=start)
    return Path(result.stdout.strip())


def tracked_python_tests(repo: Path) -> tuple[str, ...]:
    """Return local tracked ``*_test.py`` paths, without filesystem guesses."""
    result = _run(
        ["git", "ls-files", "-z", "--", "*_test.py"],
        cwd=repo,
        text=False,
    )
    return tuple(
        sorted(
            path.decode(errors="surrogateescape")
            for path in result.stdout.split(b"\0")
            if path
        )
    )


def bazel_test_roots(repo: Path) -> tuple[str, ...]:
    """Return every runnable test label selected by Bazel."""
    result = _run(
        [
            "bazel",
            "query",
            "--noshow_progress",
            "--ui_event_filters=-info",
            "--output=label",
            "--order_output=no",
            TEST_QUERY,
        ],
        cwd=repo,
    )
    return tuple(line for line in result.stdout.splitlines() if line.startswith("//"))


def bazel_source_graph(repo: Path) -> str:
    """Return evaluated rules and files needed to follow test ``srcs`` edges."""
    result = _run(
        [
            "bazel",
            "query",
            "--noshow_progress",
            "--ui_event_filters=-info",
            "--output=xml",
            "--order_output=no",
            SOURCE_GRAPH_QUERY,
        ],
        cwd=repo,
    )
    return result.stdout


def parse_query_graph(xml: str) -> QueryGraph:
    """Extract only source files and ``srcs`` ownership edges from query XML."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ReachabilityError(f"cannot parse bazel query XML: {error}") from error

    source_labels = frozenset(
        element.attrib["name"]
        for element in root.findall("source-file")
        if element.attrib.get("name", "").startswith("//")
    )
    src_edges: dict[str, tuple[str, ...]] = {}
    for rule in root.findall("rule"):
        name = rule.attrib.get("name")
        if not name or not name.startswith("//"):
            continue
        srcs = rule.find("list[@name='srcs']")
        if srcs is None:
            src_edges[name] = ()
            continue
        src_edges[name] = tuple(
            label.attrib["value"]
            for label in srcs.findall("label")
            if label.attrib.get("value", "").startswith("//")
        )
    return QueryGraph(source_labels=source_labels, src_edges=src_edges)


def _label_to_path(label: str) -> str | None:
    if not label.startswith("//") or label.startswith("//external"):
        return None
    package, separator, target = label[2:].partition(":")
    if not separator:
        return None
    return f"{package}/{target}" if package else target


def reachable_source_paths(
    test_roots: Iterable[str], graph: QueryGraph
) -> frozenset[str]:
    """Follow only evaluated ``srcs`` edges from runnable test rules."""
    pending = list(test_roots)
    visited: set[str] = set()
    sources: set[str] = set()
    while pending:
        label = pending.pop()
        if label in visited:
            continue
        visited.add(label)
        if label in graph.source_labels:
            path = _label_to_path(label)
            if path is not None:
                sources.add(path)
            continue
        pending.extend(graph.src_edges.get(label, ()))
    return frozenset(sources)


def find_orphans(
    tracked_tests: Iterable[str],
    test_roots: Iterable[str],
    graph: QueryGraph,
) -> tuple[str, ...]:
    """Return tracked Python tests that no runnable test owns as source."""
    reachable = reachable_source_paths(test_roots, graph)
    return tuple(sorted(set(tracked_tests) - reachable))


def format_orphans(orphans: Sequence[str]) -> str:
    rendered = "\n".join(f"  {path}" for path in orphans)
    return (
        "tracked Python test sources are not reachable through srcs from any "
        "runnable Bazel test target:\n"
        f"{rendered}\n"
        "Add or restore a test target for each path. The check uses Bazel's "
        "evaluated graph, so a comment, data entry, scanner target, or "
        "unrelated dependency does not register a Python test."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="path inside the git worktree (default: current directory)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo = repository_root(args.workspace)
        tracked = tracked_python_tests(repo)
        roots = bazel_test_roots(repo)
        graph = parse_query_graph(bazel_source_graph(repo))
        orphans = find_orphans(tracked, roots, graph)
    except ReachabilityError as error:
        print(f"test-source-reachability: ERROR: {error}", file=sys.stderr)
        return 2

    if orphans:
        print(
            "test-source-reachability: ERROR: " + format_orphans(orphans),
            file=sys.stderr,
        )
        return 1
    print(
        "test-source-reachability: PASS: "
        f"all {len(tracked)} tracked *_test.py files are runnable sources"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
