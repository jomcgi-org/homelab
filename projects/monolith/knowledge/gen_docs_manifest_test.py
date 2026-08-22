import json
import subprocess
from pathlib import Path

import pytest

from knowledge.tools.gen_docs_manifest import (
    build_manifest,
    derive_title,
    iter_doc_paths,
    main,
    make_slug,
    require_public_readmes,
)


def _write(root: Path, rel_path: str, content: str = "# Document\n\nBody") -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _track(root: Path, *rel_paths: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    if rel_paths:
        subprocess.run(["git", "add", "--", *rel_paths], cwd=root, check=True)


def _tracked_manifest(root: Path) -> list[dict]:
    return build_manifest(root, iter_doc_paths(root))


def test_derive_title_prefers_h1():
    assert derive_title("# Hello\n\nbody", "projects/embervm/README.md") == "Hello"


def test_derive_title_falls_back_for_readme_and_named_doc():
    assert derive_title("no heading", "projects/embervm/README.md") == "embervm"
    assert (
        derive_title("no heading", "projects/embervm/ARCHITECTURE.md") == "ARCHITECTURE"
    )


def test_derive_title_strips_trailing_git_sha():
    assert (
        derive_title(
            "# STPA Control Analysis: EmberVM @ 55ca7188a\n\nbody",
            "projects/embervm/STPA.md",
        )
        == "STPA Control Analysis: EmberVM"
    )
    assert (
        derive_title("# Keep @ abcdef\n\nbody", "projects/embervm/README.md")
        == "Keep @ abcdef"
    )


def test_make_slug_uses_project_root_for_readme_and_kind_for_other_docs():
    assert make_slug("embervm", "readme") == "embervm"
    assert make_slug("embervm", "architecture") == "embervm/architecture"
    assert make_slug("monolith", "stpa") == "monolith/stpa"


def test_project_with_only_readme_yields_one_entry(tmp_path: Path):
    rel_path = "projects/embervm/README.md"
    _write(tmp_path, rel_path, "# EmberVM\n\nRuntime docs")
    _track(tmp_path, rel_path)

    entries = _tracked_manifest(tmp_path)

    assert len(entries) == 1
    assert entries[0]["slug"] == "embervm"


def test_missing_project_readme_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _track(tmp_path)
    monkeypatch.setenv("BUILD_WORKSPACE_DIRECTORY", str(tmp_path))
    with pytest.raises(SystemExit, match="README.md"):
        require_public_readmes(set())
    with pytest.raises(SystemExit, match="README.md"):
        main()


def test_manifest_orders_projects_then_document_kinds(tmp_path: Path):
    docs = {
        "projects/platform/STPA.md": "# Platform STPA",
        "projects/mcp/ARCHITECTURE.md": "# MCP Architecture",
        "projects/embervm/STPA.md": "# EmberVM STPA",
        "projects/embervm/README.md": "# EmberVM",
        "projects/mcp/README.md": "# MCP",
        "projects/embervm/ARCHITECTURE.md": "# EmberVM Architecture",
    }
    for path, content in docs.items():
        _write(tmp_path, path, content)
    _track(tmp_path, *reversed(tuple(docs)))

    entries = _tracked_manifest(tmp_path)

    assert [(entry["project"], entry["kind"]) for entry in entries] == [
        ("embervm", "readme"),
        ("embervm", "architecture"),
        ("embervm", "stpa"),
        ("mcp", "readme"),
        ("mcp", "architecture"),
        ("platform", "stpa"),
    ]
    assert [entry["order"] for entry in entries] == list(range(6))


def test_entries_carry_project_and_kind_without_section(tmp_path: Path):
    rel_path = "projects/monolith/STPA.md"
    _write(tmp_path, rel_path, "# Monolith STPA\n\nAnalysis")
    _track(tmp_path, rel_path)

    entry = _tracked_manifest(tmp_path)[0]

    assert entry["project"] == "monolith"
    assert entry["kind"] == "stpa"
    assert "section" not in entry
    assert set(entry) == {
        "path",
        "slug",
        "project",
        "kind",
        "title",
        "order",
        "content",
    }


def test_nested_readme_is_not_indexed(tmp_path: Path):
    rel_path = "projects/embervm/runtimes/k3s/drill/README.md"
    _write(tmp_path, rel_path)
    _track(tmp_path, rel_path)
    assert _tracked_manifest(tmp_path) == []


def test_decisions_are_not_indexed(tmp_path: Path):
    rel_path = "docs/decisions/embervm/001-example.md"
    _write(tmp_path, rel_path)
    _track(tmp_path, rel_path)
    assert _tracked_manifest(tmp_path) == []


def test_build_manifest_strips_nul_bytes(tmp_path: Path):
    rel_path = "projects/sextant/README.md"
    _write(tmp_path, rel_path, "# Sextant\n\nbe\x00fore")
    _track(tmp_path, rel_path)

    entry = _tracked_manifest(tmp_path)[0]

    assert "\x00" not in entry["content"]
    assert entry["content"] == "# Sextant\n\nbefore"


def test_build_manifest_skips_deleted_tracked_path(tmp_path: Path):
    rel_path = "projects/model-bench/README.md"
    _write(tmp_path, rel_path)
    _track(tmp_path, rel_path)
    paths = iter_doc_paths(tmp_path)
    (tmp_path / rel_path).unlink()

    assert build_manifest(tmp_path, paths) == []


def test_manifest_json_round_trips(tmp_path: Path):
    rel_path = "projects/platform/README.md"
    _write(tmp_path, rel_path, "# Platform\n\nCurrent state")
    _track(tmp_path, rel_path)
    entries = _tracked_manifest(tmp_path)
    assert json.loads(json.dumps(entries)) == entries
