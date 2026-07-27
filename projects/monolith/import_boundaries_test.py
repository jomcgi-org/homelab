"""Guard: domains may only import each other via <domain>.api.

A domain (a top-level package in DOMAINS) may import another domain only
through that domain's ``api`` module. It may not import another domain's
internal modules. A domain's own internals remain freely importable within the
domain, and the domain-agnostic ``shared``/``app`` packages are importable
anywhere.

See docs/decisions/platform/008-monolith-module-boundaries.md.
"""

import ast
import pathlib

import pytest

DOMAINS = {
    "ships",
    "stars",
    "chat",
    "chat_public",
    "knowledge",
    "hikes",
    "dr_jobs",
    "trips",
    "home",
    "scheduler",
    "agent",
    "goosecracker",
    "worldcup",
    "agent_sessions",
}

# Documented exceptions only, as (importing_domain, imported_module). Keep this
# empty; an entry here is a deliberate, reviewed hole in the boundary.
ALLOW: set[tuple[str, str]] = set()

ROOT = pathlib.Path(__file__).resolve().parent


def _domain_of(path: pathlib.Path) -> str | None:
    parts = path.relative_to(ROOT).parts
    return parts[0] if parts and parts[0] in DOMAINS else None


def _is_test_path(path: pathlib.Path) -> bool:
    s = str(path)
    return (
        path.name.endswith("_test.py")
        or path.name.startswith("test_")
        or "/tests/" in s
    )


def _violations() -> list[str]:
    out: list[str] = []
    for py in ROOT.rglob("*.py"):
        if _is_test_path(py):
            continue
        owner = _domain_of(py)
        if owner is None:
            continue
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods.extend(alias.name for alias in node.names)
            for mod in mods:
                target = mod.split(".")[0]
                if target not in DOMAINS or target == owner:
                    continue
                if mod == f"{target}.api" or mod.startswith(f"{target}.api."):
                    continue
                if (owner, mod) in ALLOW:
                    continue
                out.append(f"{py.relative_to(ROOT)}: imports {mod} (use {target}.api)")
    return out


def test_no_cross_domain_internal_imports():
    violations = _violations()
    if violations:
        pytest.fail(
            "Cross-domain boundary violations:\n" + "\n".join(sorted(violations))
        )
