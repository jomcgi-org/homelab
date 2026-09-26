#!/usr/bin/env python3
"""Reject new instances of patterns that have broken production here.

Usage:
    python3 bazel/tools/ci/source_ratchet.py BASE_REF [HEAD_REF]
    python3 bazel/tools/ci/source_ratchet.py --hook < claude-hook-json

Each rule below replaces a custom Semgrep rule removed in #4777 and backs a
gotcha in AGENTS.md. The check is a ratchet: it collects findings from every
file the PR changes, at BASE and at HEAD, and fails only on findings HEAD adds.
Existing instances are grandfathered until someone removes them.

A finding is keyed by what it is, not by its line: the enclosing function and
the method for Python rules, the image or hostname or command for the others.
So reformatting, re-wrapping, or moving code between changed files does not
create a "new" finding, while a genuinely new call does.

A line may opt out with a comment naming the rule and a reason, for example
`# ratchet-allow: svc-url (test fixture for the resolver)`. Reviewers see
every opt-out in the diff.

Rules:
  image-digest      `@sha256:` pinned on one of this repo's own images
                    (ghcr.io/jomcgi/...) in YAML. Build-time pinning replaces
                    tags; a hand pin goes stale into ImagePullBackOff.
                    Third-party digest pins and `digest:` placeholders are fine.
  svc-url           An in-cluster Service DNS name in Go, Python, JS or TS
                    source (not tests, not comments). Helm prepends the
                    release name, so a rename silently breaks it.
  sync-session      `session.execute|exec|scalars|commit|flush(...)` inside
                    `async def` and not under an `await`: it blocks the loop.
  session-to-thread `session` handed to `to_thread` or `run_in_executor`:
                    Sessions are not thread-safe.
  session-add-loop  `session.add(...)` inside a loop with no per-iteration
                    commit, flush or `begin_nested()`.
  kubectl-mutate    A kubectl or helm command that writes to the cluster, in a
                    shell script. The cluster is GitOps; change git instead.

`--hook` runs only the kubectl/helm check on a Claude Code PreToolUse payload
and exits 2 to block, so the hook and the script rule share one parser.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import json
import re
import shlex
import subprocess
import sys
from typing import Callable, Iterator, Sequence

ALLOW = re.compile(r"ratchet-allow:\s*([a-z-]+)")

EXPLAIN = {
    "image-digest": "hand-pinned digest on a repo-built image; remove it, the build pins tags",
    "svc-url": "hardcoded in-cluster URL; read it from an env var set in values.yaml",
    "sync-session": "sync Session I/O inside async def; move it into asyncio.to_thread with a fresh Session",
    "session-to-thread": "Session passed across threads; pass plain data and open a Session in the worker",
    "session-add-loop": "session.add in a loop; build the rows and session.add_all(...) once",
    "kubectl-mutate": "cluster write in a script; the cluster is GitOps, change git instead",
}


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int
    text: str
    ident: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.ident)


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{path}"
        or "/testdata/" in f"/{path}"
        or "/fixtures/" in f"/{path}"
        or name.startswith("test_")
        or name == "conftest.py"
        or re.search(r"(_test|\.test|\.spec)\.[a-z]+$", name) is not None
    )


def _finding(rule: str, lines: list[str], lineno: int, ident: str) -> Finding | None:
    text = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
    if any(m.group(1) == rule for m in ALLOW.finditer(text)):
        return None
    return Finding(rule, lineno, text.strip(), ident)


# --- image-digest ---------------------------------------------------------

_OURS = "ghcr.io/jomcgi/"
_KEY = re.compile(r"^(\s*)(?:-\s+)?([A-Za-z_]+):\s*[\"']?([^\"'\s#]*)")


def _col(line: str) -> int:
    """Column where a YAML line's key starts, treating `- ` as indentation."""
    return len(line) - len(line.lstrip(" -"))


def _mapping_siblings(lines: list[str], i: int) -> dict[str, str]:
    """Keys and scalar values of the YAML mapping that line `i` belongs to."""
    col = _col(lines[i])
    out: dict[str, str] = {}
    for step in (-1, 1):
        j = i
        while 0 <= j < len(lines):
            line = lines[j]
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if _col(line) < col:
                    break
                starts_item = stripped.startswith("- ")
                # A new list item at this column is a different mapping.
                if j != i and step == 1 and starts_item and _col(line) == col:
                    break
                if _col(line) == col:
                    m = _KEY.match(line)
                    if m:
                        out.setdefault(m.group(2), m.group(3))
                if step == -1 and starts_item and _col(line) == col:
                    break
            j += step
    return out


def image_digest(path: str, text: str) -> Iterator[Finding]:
    if not path.endswith((".yaml", ".yml")) or "lock" in path.rsplit("/", 1)[-1]:
        return
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "@sha256:" not in line or line.lstrip().startswith("#"):
            continue
        ours = _OURS in line
        if not ours:
            sib = _mapping_siblings(lines, i)
            repo = sib.get("repository", "")
            registry = sib.get("registry", "")
            ours = repo.startswith(_OURS) or (
                registry.rstrip("/") == "ghcr.io" and repo.startswith("jomcgi/")
            )
        if ours:
            ident = re.sub(r"\s+|#.*$", "", line)
            f = _finding("image-digest", lines, i + 1, ident)
            if f:
                yield f


# --- svc-url --------------------------------------------------------------

# Split so this file does not match its own rule.
_SVC_SUFFIX = ".svc" + ".cluster.local"
_SVC_HOST = re.compile(r"[\w.-]*" + re.escape(_SVC_SUFFIX))
_SRC = (".go", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".svelte")
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*)")


def svc_url(path: str, text: str) -> Iterator[Finding]:
    if not path.endswith(_SRC) or _is_test_path(path):
        return
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _SVC_SUFFIX in line and not _COMMENT.match(line):
            for host in _SVC_HOST.findall(line):
                f = _finding("svc-url", lines, i + 1, host)
                if f:
                    yield f


# --- kubectl-mutate -------------------------------------------------------

_KUBECTL_WRITES = {
    "apply", "patch", "edit", "scale", "delete", "replace", "set", "label",
    "annotate", "cordon", "uncordon", "drain", "taint", "run", "expose",
    "autoscale", "cp", "certificate",
}  # fmt: skip
_ROLLOUT_WRITES = {"restart", "undo", "pause", "resume"}
_HELM_WRITES = {"install", "upgrade", "uninstall", "delete", "rollback"}
# Global flags that take a separate value, so the value is not read as a verb.
_VALUE_FLAGS = {
    "-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user",
    "-s", "--server", "--as", "--as-group", "--token", "--request-timeout",
    "--kube-context", "--kube-apiserver", "--kube-token",
}  # fmt: skip
_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n", "|&"}
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)


def _segments(command: str) -> Iterator[list[str]]:
    """Split a shell command into simple commands, recursing into `sh -c`."""
    lexer = shlex.shlex(
        _HEREDOC.sub("", command), posix=True, punctuation_chars=";&|()"
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return
    current: list[str] = []
    for tok in tokens + [";"]:
        # Newlines separate commands; they survive as part of tokens here.
        parts = tok.split("\n")
        for n, part in enumerate(parts):
            if n > 0 or part in _SEPARATORS:
                yield from _emit(current)
                current = []
            if part and part not in _SEPARATORS:
                current.append(part)
    yield from _emit(current)


def _emit(current: list[str]) -> Iterator[list[str]]:
    if not current:
        return
    if (
        len(current) >= 3
        and current[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh"}
        and current[1] == "-c"
    ):
        yield from _segments(current[2])
    else:
        yield current


def _verb_args(args: list[str]) -> list[str]:
    """Drop global flags (and their values) that precede the subcommand."""
    out: list[str] = []
    skip = False
    for i, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if not out and arg.startswith("-"):
            if arg in _VALUE_FLAGS:
                skip = True
            continue
        out.append(arg)
    return out


def cluster_write(segment: list[str]) -> str | None:
    """The offending command if this simple command writes to the cluster."""
    while segment and (
        re.match(r"^\w+=", segment[0])
        or segment[0] in {"sudo", "env", "command", "exec", "time"}
    ):
        segment = segment[1:]
    if not segment:
        return None
    tool = segment[0].rsplit("/", 1)[-1]
    if any(a.startswith("--dry-run") for a in segment):
        return None
    rest = _verb_args(segment[1:])
    if not rest:
        return None
    verb = rest[0]
    if tool == "kubectl":
        if verb in _KUBECTL_WRITES:
            return f"kubectl {verb}"
        if verb == "rollout" and len(rest) > 1 and rest[1] in _ROLLOUT_WRITES:
            return f"kubectl rollout {rest[1]}"
        if verb == "create" and not (len(rest) > 1 and rest[1] == "token"):
            return "kubectl create"
    if tool == "helm" and verb in _HELM_WRITES:
        return f"helm {verb}"
    return None


def command_writes(command: str) -> list[str]:
    return [w for seg in _segments(command) if (w := cluster_write(seg))]


def kubectl_mutate(path: str, text: str) -> Iterator[Finding]:
    if not path.endswith((".sh", ".bash")) or _is_test_path(path):
        return
    lines = text.splitlines()
    # Blank out heredoc bodies but keep line numbering.
    stripped = _HEREDOC.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    line_no = 1
    # One logical command per chunk: split on newlines not escaped by `\`.
    for chunk in re.split(r"(?<!\\)\n", stripped):
        flat = " ".join(chunk.replace("\\\n", " ").split())
        for write in command_writes(flat):
            f = _finding("kubectl-mutate", lines, line_no, f"{write}|{flat}")
            if f:
                yield f
        line_no += chunk.count("\n") + 1


# --- Python Session rules -------------------------------------------------

_SYNC_METHODS = {"execute", "exec", "scalars", "commit", "flush"}
_SESSION_NAMES = {"session", "db_session", "sess"}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# Calls whose arguments are coroutines being scheduled, not run synchronously.
_SCHEDULERS = {"gather", "create_task", "ensure_future", "wait_for", "shield", "wait"}


def _session_call(node: ast.AST, methods: set[str]) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _SESSION_NAMES
        and node.func.attr in methods
    ):
        return node.func.attr
    return None


def _walk_same_scope(nodes: list[ast.AST]) -> Iterator[ast.AST]:
    """Walk statements without entering nested function or class scopes."""
    stack = [n for n in reversed(nodes) if not isinstance(n, _SCOPES)]
    while stack:
        node = stack.pop()
        yield node
        for child in reversed(list(ast.iter_child_nodes(node))):
            if not isinstance(child, _SCOPES):
                stack.append(child)


def _call_name(node: ast.Call) -> str:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")


def _qualnames(tree: ast.AST) -> dict[int, str]:
    out: dict[int, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                out[id(child)] = name
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def python_session(path: str, text: str) -> Iterator[Finding]:
    if not path.endswith(".py") or _is_test_path(path):
        return
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    lines = text.splitlines()
    names = _qualnames(tree)
    module = path.rsplit("/", 1)[-1].removesuffix(".py")

    # Anything under an `await`, or passed to a coroutine scheduler, is async.
    deferred: set[int] = set()
    for node in ast.walk(tree):
        roots: list[ast.AST] = []
        if isinstance(node, ast.Await):
            roots = [node.value]
        elif isinstance(node, ast.Call) and _call_name(node) in _SCHEDULERS:
            roots = list(node.args)
        for root in roots:
            deferred.update(id(n) for n in ast.walk(root))

    def scope_of(fn: ast.AST | None) -> str:
        return names.get(id(fn), module) if fn is not None else module

    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    owner: dict[int, ast.AST] = {}
    for fn in functions:
        for inner in _walk_same_scope(fn.body):
            owner[id(inner)] = fn

    for fn in functions:
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for inner in _walk_same_scope(fn.body):
            method = _session_call(inner, _SYNC_METHODS)
            if method and id(inner) not in deferred:
                f = _finding(
                    "sync-session", lines, inner.lineno, f"{scope_of(fn)}:{method}"
                )
                if f:
                    yield f

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in {
            "to_thread",
            "run_in_executor",
        }:
            values = list(node.args) + [k.value for k in node.keywords]
            if any(isinstance(v, ast.Name) and v.id == "session" for v in values):
                ident = f"{scope_of(owner.get(id(node)))}:{_call_name(node)}"
                f = _finding("session-to-thread", lines, node.lineno, ident)
                if f:
                    yield f

        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            body = list(_walk_same_scope(node.body))
            adds = [n for n in body if _session_call(n, {"add"})]
            if not adds:
                continue
            per_iteration = any(
                _session_call(n, {"commit", "flush", "begin_nested"}) for n in body
            ) or any(
                isinstance(n, ast.Call)
                and any(
                    isinstance(a, ast.Attribute) and a.attr == "commit" for a in n.args
                )
                for n in body
            )
            if not per_iteration:
                for add in adds:
                    ident = f"{scope_of(owner.get(id(add)))}:add"
                    f = _finding("session-add-loop", lines, add.lineno, ident)
                    if f:
                        yield f


SCANNED = (".yaml", ".yml", ".sh", ".bash") + _SRC

RULES: list[Callable[[str, str], Iterator[Finding]]] = [
    image_digest,
    svc_url,
    kubectl_mutate,
    python_session,
]


def scan(path: str, text: str | None) -> list[Finding]:
    if text is None:
        return []
    out: list[Finding] = []
    for rule in RULES:
        out.extend(rule(path, text))
    return out


def new_findings(
    before: Sequence[tuple[str, str | None]], after: Sequence[tuple[str, str | None]]
) -> list[tuple[str, Finding]]:
    """Findings across `after` files beyond those across `before` files."""
    budget = Counter(f.key for path, text in before for f in scan(path, text))
    out = []
    for path, text in after:
        for f in scan(path, text):
            if budget[f.key] > 0:
                budget[f.key] -= 1
            else:
                out.append((path, f))
    return out


def _git_read(ref: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _changes(base: str, head: str) -> list[tuple[str | None, str | None]]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "-M", base, head],
        capture_output=True,
        text=True,
        check=True,
    )
    out: list[tuple[str | None, str | None]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status in "RC":
            out.append((parts[1], parts[2]))
        elif status == "A":
            out.append((None, parts[1]))
        elif status == "D":
            out.append((parts[1], None))
        else:
            out.append((parts[1], parts[1]))
    return out


def _hook() -> int:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    writes = command_writes(command)
    if not writes:
        return 0
    print(
        f"BLOCKED: {', '.join(sorted(set(writes)))}. kubectl and helm are read-only "
        "here: the cluster is GitOps.\n\nTo change something, edit "
        "projects/<service>/deploy/values.yaml (or the chart), commit, push, and "
        "let ArgoCD sync it. Read-only verbs and --dry-run are fine.",
        file=sys.stderr,
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", nargs="?")
    parser.add_argument("head", nargs="?", default="HEAD")
    parser.add_argument("--hook", action="store_true")
    args = parser.parse_args(argv)
    if args.hook:
        return _hook()
    if not args.base:
        parser.error("BASE_REF is required")

    before, after = [], []
    for old, new in _changes(args.base, args.head):
        if old and old.endswith(SCANNED):
            before.append((old, _git_read(args.base, old)))
        if new and new.endswith(SCANNED):
            after.append((new, _git_read(args.head, new)))
    problems = new_findings(before, after)
    if not problems:
        return 0
    print(
        "New instances of patterns that have broken production (see AGENTS.md, "
        "Gotchas). Fix them, or add `ratchet-allow: <rule> (<reason>)` on the "
        "line if it is genuinely safe:",
        file=sys.stderr,
    )
    for path, f in problems:
        print(f"  {path}:{f.line}: [{f.rule}] {EXPLAIN[f.rule]}", file=sys.stderr)
        print(f"      {f.text}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
