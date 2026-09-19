"""Guard the documented cross-domain API boundary.

The explicit ``DOMAINS`` set is the scope of this contract. Every non-test
Python source below those package roots is listed by a Bazel-generated manifest
and materialised as a runfile. The check fails closed if the manifest, a domain,
or any expected source is absent, or if the runfiles contain an unmanifested
source. This is intentionally not a claim that every top-level monolith package
is an architecture domain.

Absolute imports of another covered domain must enter through its ``api``
module. Aliases do not change that rule. Relative imports are treated as
owner-local because these domains are top-level packages, so Python has no
valid relative syntax for reaching a sibling domain.

See projects/monolith/ARCHITECTURE.md, section 2.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterable

import pytest

DOMAINS = frozenset(
    {
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
        "factory",
        "moving",
    }
)

# Documented exceptions only, as (importing_domain, imported_module). Keep this
# empty; an entry here is a deliberate, reviewed hole in the boundary.
ALLOW: frozenset[tuple[str, str]] = frozenset()

ROOT = pathlib.Path(__file__).resolve().parent
MANIFEST = ROOT / "import_boundary_sources_manifest.txt"


class CoverageError(AssertionError):
    """The declared boundary source coverage is unavailable or incomplete."""


def _domain_of(
    path: pathlib.Path,
    root: pathlib.Path = ROOT,
    domains: frozenset[str] = DOMAINS,
) -> str | None:
    parts = path.relative_to(root).parts
    return parts[0] if parts and parts[0] in domains else None


def _is_test_path(path: pathlib.Path) -> bool:
    return (
        path.name.endswith("_test.py")
        or path.name.startswith("test_")
        or "tests" in path.parts
    )


def _read_manifest(manifest: pathlib.Path) -> frozenset[pathlib.PurePosixPath]:
    try:
        lines = manifest.read_text().splitlines()
    except OSError as exc:
        raise CoverageError(f"boundary source manifest unavailable: {manifest}") from exc

    sources: set[pathlib.PurePosixPath] = set()
    for raw in lines:
        value = raw.strip()
        if not value:
            continue
        path = pathlib.PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise CoverageError(f"invalid boundary source manifest entry: {value!r}")
        if path in sources:
            raise CoverageError(f"duplicate boundary source manifest entry: {value}")
        sources.add(path)

    if not sources:
        raise CoverageError("boundary source manifest is empty")
    return frozenset(sources)


def _discover_sources(
    root: pathlib.Path,
    domains: frozenset[str],
) -> frozenset[pathlib.PurePosixPath]:
    sources: set[pathlib.PurePosixPath] = set()
    for domain in sorted(domains):
        domain_root = root / domain
        if not domain_root.is_dir():
            raise CoverageError(f"boundary domain runfiles unavailable: {domain}/")
        for source in domain_root.rglob("*.py"):
            relative = source.relative_to(root)
            if not _is_test_path(relative):
                sources.add(pathlib.PurePosixPath(relative.as_posix()))
    return frozenset(sources)


def _covered_sources(
    root: pathlib.Path = ROOT,
    manifest: pathlib.Path = MANIFEST,
    domains: frozenset[str] = DOMAINS,
) -> tuple[pathlib.Path, ...]:
    if not domains:
        raise CoverageError("boundary domain set is empty")

    expected = _read_manifest(manifest)
    out_of_scope = sorted(path for path in expected if path.parts[0] not in domains)
    if out_of_scope:
        rendered = ", ".join(str(path) for path in out_of_scope)
        raise CoverageError(f"manifest contains sources outside DOMAINS: {rendered}")

    covered_domains = {path.parts[0] for path in expected}
    missing_domains = sorted(domains - covered_domains)
    if missing_domains:
        raise CoverageError(
            "boundary manifest has no sources for domains: " + ", ".join(missing_domains)
        )

    missing_inits = sorted(
        pathlib.PurePosixPath(domain, "__init__.py")
        for domain in domains
        if pathlib.PurePosixPath(domain, "__init__.py") not in expected
    )
    if missing_inits:
        rendered = ", ".join(str(path) for path in missing_inits)
        raise CoverageError(f"boundary manifest is missing package sources: {rendered}")

    actual = _discover_sources(root, domains)
    missing_runfiles = sorted(expected - actual)
    unmanifested_runfiles = sorted(actual - expected)
    if missing_runfiles or unmanifested_runfiles:
        details: list[str] = []
        if missing_runfiles:
            details.append(
                "missing expected runfiles: "
                + ", ".join(str(path) for path in missing_runfiles)
            )
        if unmanifested_runfiles:
            details.append(
                "unmanifested domain sources: "
                + ", ".join(str(path) for path in unmanifested_runfiles)
            )
        raise CoverageError("; ".join(details))

    return tuple(root / pathlib.Path(*path.parts) for path in sorted(expected))


def _imported_modules(
    node: ast.Import | ast.ImportFrom,
    domains: frozenset[str],
) -> Iterable[str]:
    if isinstance(node, ast.Import):
        return (alias.name for alias in node.names)

    if node.level:
        return ()
    if not node.module:
        return ()
    if node.module in domains:
        return (f"{node.module}.{alias.name}" for alias in node.names)
    return (node.module,)


def _violations(
    sources: Iterable[pathlib.Path],
    root: pathlib.Path = ROOT,
    domains: frozenset[str] = DOMAINS,
) -> list[str]:
    out: list[str] = []
    for source in sources:
        owner = _domain_of(source, root, domains)
        if owner is None:
            raise CoverageError(f"covered source has no domain owner: {source}")
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for mod in _imported_modules(node, domains):
                target = mod.split(".")[0]
                if target not in domains or target == owner:
                    continue
                if mod == f"{target}.api" or mod.startswith(f"{target}.api."):
                    continue
                if (owner, mod) in ALLOW:
                    continue
                out.append(
                    f"{source.relative_to(root)}:{node.lineno}: imports {mod} "
                    f"(use {target}.api)"
                )
    return out


def _write_source(root: pathlib.Path, relative: str, source: str = "") -> pathlib.Path:
    """Create a source tree for focused checker tests."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_no_cross_domain_internal_imports() -> None:
    sources = _covered_sources()
    violations = _violations(sources)
    if violations:
        pytest.fail(
            "Cross-domain boundary violations:\n" + "\n".join(sorted(violations))
        )


def test_detects_realistic_cross_domain_import_forms(tmp_path: pathlib.Path) -> None:
    domains = frozenset({"auth", "knowledge"})
    auth_init = _write_source(tmp_path, "auth/__init__.py")
    knowledge_init = _write_source(tmp_path, "knowledge/__init__.py")
    caller = _write_source(
        tmp_path,
        "auth/service.py",
        """\
import knowledge.api as knowledge_api
from knowledge import api as alternate_api
from knowledge.api import search_notes
from . import models

import knowledge.store as store
from knowledge import models as knowledge_models
""",
    )

    assert _violations(
        [auth_init, caller, knowledge_init], tmp_path, domains
    ) == [
        "auth/service.py:6: imports knowledge.store (use knowledge.api)",
        "auth/service.py:7: imports knowledge.models (use knowledge.api)",
    ]


def test_missing_manifest_cannot_pass(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CoverageError, match="manifest unavailable"):
        _covered_sources(
            tmp_path,
            tmp_path / "missing.txt",
            frozenset({"auth"}),
        )


def test_empty_manifest_cannot_pass(tmp_path: pathlib.Path) -> None:
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("")
    with pytest.raises(CoverageError, match="manifest is empty"):
        _covered_sources(tmp_path, manifest, frozenset({"auth"}))


def test_missing_domain_coverage_cannot_pass(tmp_path: pathlib.Path) -> None:
    _write_source(tmp_path, "auth/__init__.py")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("auth/__init__.py\n")
    with pytest.raises(CoverageError, match="no sources for domains: knowledge"):
        _covered_sources(tmp_path, manifest, frozenset({"auth", "knowledge"}))


def test_missing_expected_runfile_cannot_pass(tmp_path: pathlib.Path) -> None:
    _write_source(tmp_path, "auth/__init__.py")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("auth/__init__.py\nauth/service.py\n")
    with pytest.raises(CoverageError, match="missing expected runfiles: auth/service.py"):
        _covered_sources(tmp_path, manifest, frozenset({"auth"}))


def test_partial_manifest_cannot_pass(tmp_path: pathlib.Path) -> None:
    _write_source(tmp_path, "auth/__init__.py")
    _write_source(tmp_path, "auth/service.py")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("auth/__init__.py\n")
    with pytest.raises(CoverageError, match="unmanifested domain sources: auth/service.py"):
        _covered_sources(tmp_path, manifest, frozenset({"auth"}))
