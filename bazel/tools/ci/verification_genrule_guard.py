"""Require verification tags on suite-shaped genrules in BUILD files.

The guard deliberately uses the issue's broad name heuristic: ``tlc_*``,
``*_smoke``, and ``*_test``. It can miss a suite with another name, and a
build-only genrule with a suite-shaped name must either be renamed or tagged.
There is no fixed target inventory, so BUILD files in new packages are covered.

Repository BUILD files use the Python-compatible Starlark syntax accepted by
``ast.parse``. A file outside that syntax overlap is a hard error instead of an
unscanned success. Only a literal ``verification`` entry in a literal tags list
is accepted, which keeps computed or conditional tags from passing by accident.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SUITE_NAME = re.compile(r"^(?:tlc_.*|.*_(?:smoke|test))$")
BUILD_FILENAMES = frozenset({"BUILD", "BUILD.bazel"})


class GuardError(Exception):
    """Raised when the repository cannot be scanned completely."""


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str
    name: str


@dataclass(frozen=True)
class ScanResult:
    files_scanned: int
    findings: tuple[Finding, ...]


def discover_build_files(source_root: Path) -> list[Path]:
    """Return every exact BUILD or BUILD.bazel below source_root."""
    discovered: list[Path] = []
    for directory, dirnames, filenames in os.walk(source_root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        directory_path = Path(directory)
        for filename in sorted(BUILD_FILENAMES.intersection(filenames)):
            discovered.append(directory_path / filename)
    return discovered


def _genrule_kind(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name) and call.func.id == "genrule":
        return "genrule"
    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "genrule"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "native"
    ):
        return "native.genrule"
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next(
        (keyword.value for keyword in call.keywords if keyword.arg == name),
        None,
    )


def _literal_string(expression: ast.expr | None) -> str | None:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    return None


def _has_literal_verification_tag(expression: ast.expr | None) -> bool:
    if not isinstance(expression, (ast.List, ast.Tuple)):
        return False
    return any(
        _literal_string(element) == "verification" for element in expression.elts
    )


def scan_build_file(path: Path, source_root: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GuardError(f"{path}: could not read BUILD file: {exc}") from exc

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno or 1
        raise GuardError(
            f"{path.relative_to(source_root)}:{line}: "
            f"could not parse BUILD file: {exc.msg}"
        ) from exc

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _genrule_kind(node)
        if kind is None:
            continue
        name = _literal_string(_keyword(node, "name"))
        if name is None or SUITE_NAME.fullmatch(name) is None:
            continue
        if _has_literal_verification_tag(_keyword(node, "tags")):
            continue
        findings.append(
            Finding(
                path=path.relative_to(source_root),
                line=node.lineno,
                kind=kind,
                name=name,
            )
        )
    return findings


def scan_repository(source_root: Path) -> ScanResult:
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise GuardError(f"{source_root}: source root is not a directory")

    build_files = discover_build_files(source_root)
    if not build_files:
        raise GuardError(f"{source_root}: no BUILD or BUILD.bazel files found")

    findings: list[Finding] = []
    for path in build_files:
        findings.extend(scan_build_file(path, source_root))
    return ScanResult(files_scanned=len(build_files), findings=tuple(findings))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Require verification tags on suite-shaped genrules.",
    )
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args(argv)

    try:
        result = scan_repository(args.source_root)
    except GuardError as exc:
        print(
            f"ERROR: verification genrule guard could not scan: {exc}", file=sys.stderr
        )
        return 2

    for finding in result.findings:
        print(
            f"ERROR: {finding.path}:{finding.line}: {finding.kind} "
            f"{finding.name!r} must include the literal verification tag",
            file=sys.stderr,
        )
    if result.findings:
        print(
            f"FAILED: scanned {result.files_scanned} BUILD file(s), "
            f"found {len(result.findings)} suite-shaped genrule(s) without "
            "verification",
            file=sys.stderr,
        )
        return 1

    print(
        f"PASSED: scanned {result.files_scanned} BUILD file(s), "
        "no missing verification tags",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
