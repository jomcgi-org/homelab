#!/usr/bin/env python3
"""Reject new central monolith package enumeration.

Usage:
    python3 bazel/tools/ci/central_build_ratchet.py BASE_REF [HEAD_REF]

The checker compares semantic entries instead of totals. Existing entries in
projects/monolith/BUILD are grandfathered only in their existing assignment or
target attribute. Deletions are allowed, while additions, duplicate additions,
replacements, and moves to another central list are rejected. New packages
should own ordinary Bazel targets in their package-local BUILD file.

There is intentionally no command-line bypass. A future exception requires a
documented, narrowly scoped code change that can be reviewed in CI.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence

CENTRAL_BUILD = "projects/monolith/BUILD"
_GAZELLE_EXCLUDE = re.compile(r"^\s*#\s*gazelle:exclude(?:\s+(?P<pattern>\S+))?\s*$")


class RatchetError(RuntimeError):
    """The comparison could not be performed safely."""


@dataclass(frozen=True)
class Finding:
    """One forbidden central entry and its semantic identity."""

    kind: str
    context: str
    value: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.context, self.value)

    def diagnostic(self) -> str:
        if self.kind == "gazelle:exclude":
            rendered = f"# gazelle:exclude {self.value}".rstrip()
            return f"{CENTRAL_BUILD}:{self.line}: new central directive: {rendered}"
        return (
            f"{CENTRAL_BUILD}:{self.line}: new central package glob "
            f"{self.value!r} in {self.context}"
        )


@dataclass(frozen=True)
class _StringValue:
    value: str
    line: int


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return "<call>"


def _assignment_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, (ast.Tuple, ast.List)):
        return ",".join(_assignment_name(item) for item in node.elts)
    return "<assignment>"


def _string_values(
    node: ast.AST,
    variables: dict[str, tuple[_StringValue, ...]],
) -> tuple[_StringValue, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (_StringValue(node.value, node.lineno),)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return tuple(
            value for item in node.elts for value in _string_values(item, variables)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_values(node.left, variables) + _string_values(
            node.right, variables
        )
    if isinstance(node, ast.Name):
        return variables.get(node.id, ())
    return ()


def _is_package_pattern(pattern: str) -> bool:
    """Return whether a glob centrally selects a named package subtree."""
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if "/" not in pattern:
        return False
    first_component = pattern.split("/", 1)[0]
    return first_component not in {"", ".", "..", "*", "**"}


def _glob_findings(
    node: ast.AST,
    context: str,
    variables: dict[str, tuple[_StringValue, ...]],
) -> list[Finding]:
    findings: list[Finding] = []
    if isinstance(node, ast.Call) and _call_name(node.func) == "glob":
        inputs: list[tuple[str, ast.AST]] = []
        if node.args:
            inputs.append(("include", node.args[0]))
        inputs.extend(
            (keyword.arg, keyword.value)
            for keyword in node.keywords
            if keyword.arg in {"include", "exclude"}
        )
        for role, value_node in inputs:
            for string in _string_values(value_node, variables):
                if _is_package_pattern(string.value):
                    findings.append(
                        Finding(
                            kind="package glob",
                            context=f"{context} ({role})",
                            value=string.value,
                            line=string.line,
                        )
                    )
        return findings

    for child in ast.iter_child_nodes(node):
        findings.extend(_glob_findings(child, context, variables))
    return findings


def scan_central_build(content: str) -> list[Finding]:
    """Return forbidden-pattern occurrences from a central BUILD snapshot."""
    findings = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        match = _GAZELLE_EXCLUDE.match(line)
        if match:
            findings.append(
                Finding(
                    kind="gazelle:exclude",
                    context="directive",
                    value=match.group("pattern") or "",
                    line=line_number,
                )
            )

    try:
        tree = ast.parse(content, filename=CENTRAL_BUILD)
    except SyntaxError as error:
        raise RatchetError(f"cannot parse {CENTRAL_BUILD}: {error}") from error

    variables: dict[str, tuple[_StringValue, ...]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            values = _string_values(statement.value, variables)
            if values:
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        variables[target.id] = values
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            values = _string_values(statement.value, variables)
            if values:
                variables[statement.target.id] = values

    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            context = "assignment:" + ",".join(
                _assignment_name(target) for target in statement.targets
            )
            findings.extend(_glob_findings(statement.value, context, variables))
            continue
        if isinstance(statement, ast.AnnAssign):
            context = f"assignment:{_assignment_name(statement.target)}"
            findings.extend(_glob_findings(statement.value, context, variables))
            continue
        if not isinstance(statement, ast.Expr) or not isinstance(
            statement.value, ast.Call
        ):
            continue
        call = statement.value
        rule_kind = _call_name(call.func)
        target_name = next(
            (
                keyword.value.value
                for keyword in call.keywords
                if keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ),
            "<anonymous>",
        )
        for index, argument in enumerate(call.args):
            context = f"target:{rule_kind}:{target_name}:arg{index}"
            findings.extend(_glob_findings(argument, context, variables))
        for keyword in call.keywords:
            if keyword.arg == "name":
                continue
            context = f"target:{rule_kind}:{target_name}:{keyword.arg}"
            findings.extend(_glob_findings(keyword.value, context, variables))

    return findings


def new_findings(base_content: str, head_content: str) -> list[Finding]:
    """Return head occurrences not grandfathered by the base snapshot."""
    remaining = Counter(finding.key for finding in scan_central_build(base_content))
    additions = []
    for finding in scan_central_build(head_content):
        if remaining[finding.key] > 0:
            remaining[finding.key] -= 1
        else:
            additions.append(finding)
    return additions


def _git_text(repo: Path, ref: str, role: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{ref}:{CENTRAL_BUILD}"],
        cwd=repo,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git show returned no diagnostic"
        raise RatchetError(
            f"cannot read {role} reference {ref!r} at {CENTRAL_BUILD}: {detail}. "
            "Fetch the pull request base branch and pass its full ref."
        )
    return result.stdout


def check_repository(repo: Path, base_ref: str, head_ref: str) -> list[Finding]:
    """Compare two committed repository snapshots."""
    base_content = _git_text(repo, base_ref, "base")
    head_content = _git_text(repo, head_ref, "head")
    return new_findings(base_content, head_content)


def _repository_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RatchetError(
            "cannot find the repository root: "
            + (result.stderr.strip() or "git rev-parse failed")
        )
    return Path(result.stdout.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reject new gazelle:exclude directives and named-package glob "
            f"entries in {CENTRAL_BUILD}."
        )
    )
    parser.add_argument("base_ref", help="grandfathered git base reference")
    parser.add_argument(
        "head_ref",
        nargs="?",
        default="HEAD",
        help="candidate git reference (default: HEAD)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        additions = check_repository(_repository_root(), args.base_ref, args.head_ref)
    except RatchetError as error:
        print(f"ERROR: central BUILD ratchet could not run: {error}", file=sys.stderr)
        return 2

    if not additions:
        print(
            "Central BUILD ratchet passed: no new gazelle exclusions or "
            "package-specific glob entries."
        )
        return 0

    print(
        "ERROR: new central monolith BUILD enumeration is forbidden.",
        file=sys.stderr,
    )
    for finding in additions:
        print(f"  {finding.diagnostic()}", file=sys.stderr)
    print(
        "Define the package target in its own BUILD file and aggregate Bazel "
        "targets instead. Existing base entries are grandfathered only until "
        "they are removed; replacements are not allowed.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
