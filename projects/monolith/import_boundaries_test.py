"""Guard: domains may only import each other via <domain>.api.

A domain (a top-level package in DOMAINS) may import another domain only
through that domain's ``api`` module. It may not import another domain's
internal modules. A domain's own internals remain freely importable within the
domain, and the domain-agnostic ``shared``/``app`` packages are importable
anywhere.

See projects/monolith/ARCHITECTURE.md, section 2.
"""

import ast
import os
import pathlib

import pytest

DOMAINS = {
    "auth",
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
    "moving",
}

# Documented exceptions only, as (importing_domain, imported_module). Keep this
# empty; an entry here is a deliberate, reviewed hole in the boundary.
ALLOW: set[tuple[str, str]] = set()

ROOT = pathlib.Path(__file__).resolve().parent

OFFLINE_DOMAIN_SOURCES = {
    "stars/grid_gen/backfill_cerra.py",
    "stars/grid_gen/backfill_climatology.py",
    "stars/grid_gen/generate_grid.py",
    "stars/grid_gen/generate_grid_v2.py",
    "trips/backfill/__init__.py",
    "trips/backfill/main.py",
}

RUNTIME_SOURCE_SENTINELS = {
    "knowledge/api.py",
    "moving/module.py",
    "shotter/mcp.py",
}

RUNTIME_TEST_FIXTURES = {
    "knowledge/tests/__init__.py",
    "knowledge/tests/conftest.py",
    "moving/tests/__init__.py",
    "moving/tests/conftest.py",
    "shotter/tests/__init__.py",
    "shotter/tests/conftest.py",
}


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


def _scanned_domain_sources() -> tuple[pathlib.Path, ...]:
    return tuple(
        py
        for py in ROOT.rglob("*.py")
        if not _is_test_path(py) and _domain_of(py) is not None
    )


def _violations() -> list[str]:
    out: list[str] = []
    for py in _scanned_domain_sources():
        owner = _domain_of(py)
        assert owner is not None
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


def test_offline_domain_sources_are_scanned():
    scanned = {str(path.relative_to(ROOT)) for path in _scanned_domain_sources()}
    assert OFFLINE_DOMAIN_SOURCES <= scanned


def test_runtime_source_closure_excludes_test_fixtures():
    if "TEST_SRCDIR" not in os.environ:
        pytest.skip("runtime source closure is observable in Bazel runfiles")

    missing_sentinels = [
        source for source in RUNTIME_SOURCE_SENTINELS if not (ROOT / source).is_file()
    ]
    included_fixtures = [
        source for source in RUNTIME_TEST_FIXTURES if (ROOT / source).is_file()
    ]

    assert missing_sentinels == []
    assert included_fixtures == []


def test_no_cross_domain_internal_imports():
    violations = _violations()
    if violations:
        pytest.fail(
            "Cross-domain boundary violations:\n" + "\n".join(sorted(violations))
        )
