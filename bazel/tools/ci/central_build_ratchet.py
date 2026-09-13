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
_GAZELLE_EXCLUDE = re.compile(r"^\s*#\s*gazelle:exclude(?:\s+(?P<pattern>.*?))?\s*$")
_MUTATING_SEQUENCE_METHODS = {
    "append",
    "clear",
    "extend",
    "insert",
    "pop",
    "remove",
    "reverse",
    "sort",
}


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
        if self.kind == "dynamic glob":
            return (
                f"{CENTRAL_BUILD}:{self.line}: new dynamic glob expression "
                f"{self.value!r} in {self.context} cannot be verified as broad"
            )
        return (
            f"{CENTRAL_BUILD}:{self.line}: new central package glob "
            f"{self.value!r} in {self.context}"
        )


@dataclass(frozen=True)
class _StringValue:
    value: str
    line: int


@dataclass(frozen=True)
class _UnresolvedValue:
    value: str
    line: int


@dataclass(frozen=True)
class _ExpressionValue:
    kind: str | None
    strings: tuple[_StringValue, ...] = ()
    unresolved: tuple[_UnresolvedValue, ...] = ()


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


def _expression_value(
    node: ast.AST,
    variables: dict[str, _ExpressionValue],
) -> _ExpressionValue:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _ExpressionValue(
            kind="string",
            strings=(_StringValue(node.value, node.lineno),),
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        strings: list[_StringValue] = []
        unresolved: list[_UnresolvedValue] = []
        for item in node.elts:
            item_value = _expression_value(item, variables)
            if item_value.kind == "string":
                strings.extend(item_value.strings)
                unresolved.extend(item_value.unresolved)
            else:
                unresolved.extend(
                    item_value.unresolved
                    or (_UnresolvedValue(ast.unparse(item), item.lineno),)
                )
        return _ExpressionValue(
            kind="sequence",
            strings=tuple(strings),
            unresolved=tuple(unresolved),
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _expression_value(node.left, variables)
        right = _expression_value(node.right, variables)
        if left.kind == right.kind == "sequence":
            return _ExpressionValue(
                kind="sequence",
                strings=left.strings + right.strings,
                unresolved=left.unresolved + right.unresolved,
            )
        if left.kind == right.kind == "string":
            return _ExpressionValue(
                kind="string",
                strings=tuple(
                    _StringValue(left_value.value + right_value.value, node.lineno)
                    for left_value in left.strings
                    for right_value in right.strings
                ),
                unresolved=left.unresolved + right.unresolved,
            )
        return _ExpressionValue(
            kind=None,
            strings=left.strings + right.strings,
            unresolved=(_UnresolvedValue(ast.unparse(node), node.lineno),),
        )
    if isinstance(node, ast.Name):
        return variables.get(
            node.id,
            _ExpressionValue(
                kind=None,
                unresolved=(_UnresolvedValue(node.id, node.lineno),),
            ),
        )
    return _ExpressionValue(
        kind=None,
        unresolved=(_UnresolvedValue(ast.unparse(node), node.lineno),),
    )


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
    variables: dict[str, _ExpressionValue],
) -> list[Finding]:
    findings: list[Finding] = []
    if isinstance(node, ast.Call) and _call_name(node.func) == "glob":
        inputs: list[tuple[str, ast.AST]] = []
        if node.args:
            inputs.append(("include", node.args[0]))
        if len(node.args) > 1:
            inputs.append(("exclude", node.args[1]))
        inputs.extend(
            (keyword.arg, keyword.value)
            for keyword in node.keywords
            if keyword.arg in {"include", "exclude"}
        )
        for role, value_node in inputs:
            expression = _expression_value(value_node, variables)
            for string in expression.strings:
                if _is_package_pattern(string.value):
                    findings.append(
                        Finding(
                            kind="package glob",
                            context=f"{context} ({role})",
                            value=string.value,
                            line=string.line,
                        )
                    )
            for unresolved in expression.unresolved:
                findings.append(
                    Finding(
                        kind="dynamic glob",
                        context=f"{context} ({role})",
                        value=unresolved.value,
                        line=unresolved.line,
                    )
                )
        return findings

    for child in ast.iter_child_nodes(node):
        findings.extend(_glob_findings(child, context, variables))
    return findings


def _invalidate_variable_and_aliases(
    name: str,
    mutation: ast.AST,
    variables: dict[str, _ExpressionValue],
) -> None:
    """Fail closed for a mutated value and names assigned as direct aliases."""
    previous = variables.get(name)
    unresolved = _ExpressionValue(
        kind=None,
        unresolved=(_UnresolvedValue(ast.unparse(mutation), mutation.lineno),),
    )
    if previous is None:
        variables[name] = unresolved
        return
    for variable_name, value in tuple(variables.items()):
        if value is previous:
            variables[variable_name] = unresolved


def _mutated_variable(call: ast.Call) -> str | None:
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.attr in _MUTATING_SEQUENCE_METHODS
    ):
        return call.func.value.id
    return None


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

    variables: dict[str, _ExpressionValue] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            context = "assignment:" + ",".join(
                _assignment_name(target) for target in statement.targets
            )
            findings.extend(_glob_findings(statement.value, context, variables))
            value = _expression_value(statement.value, variables)
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    variables[target.id] = value
            continue
        if isinstance(statement, ast.AnnAssign):
            context = f"assignment:{_assignment_name(statement.target)}"
            findings.extend(_glob_findings(statement.value, context, variables))
            if isinstance(statement.target, ast.Name):
                variables[statement.target.id] = _expression_value(
                    statement.value, variables
                )
            continue
        if isinstance(statement, ast.AugAssign):
            context = f"assignment:{_assignment_name(statement.target)}"
            findings.extend(_glob_findings(statement.value, context, variables))
            if isinstance(statement.target, ast.Name):
                _invalidate_variable_and_aliases(
                    statement.target.id, statement, variables
                )
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
        mutated_variable = _mutated_variable(call)
        if mutated_variable is not None:
            _invalidate_variable_and_aliases(mutated_variable, call, variables)

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
