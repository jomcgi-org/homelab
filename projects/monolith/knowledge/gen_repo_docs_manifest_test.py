import json
from pathlib import Path

from knowledge.tools.gen_repo_docs_manifest import (
    derive_title,
    iter_doc_paths,
    build_manifest_lines,
)


def test_derive_title_prefers_h1():
    assert derive_title("# Hello\n\nbody", "docs/x.md") == "Hello"


def test_derive_title_falls_back_to_path():
    assert derive_title("no heading here", "docs/x.md") == "docs/x.md"


def test_iter_doc_paths_includes_and_excludes(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A")
    (tmp_path / "projects" / "svc").mkdir(parents=True)
    (tmp_path / "projects" / "svc" / "README.md").write_text("# R")
    (tmp_path / "CLAUDE.md").write_text("# Root")
    # excluded noise
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "z.md").write_text("# Z")
    (tmp_path / "projects" / "svc" / "frontend").mkdir()
    (tmp_path / "projects" / "svc" / "frontend" / "build").mkdir()
    (tmp_path / "projects" / "svc" / "frontend" / "build" / "g.md").write_text("# G")

    paths = set(iter_doc_paths(tmp_path))
    assert "docs/a.md" in paths
    assert "projects/svc/README.md" in paths
    assert "CLAUDE.md" in paths
    assert "node_modules/z.md" not in paths
    assert "projects/svc/frontend/build/g.md" not in paths


def test_build_manifest_lines_sorted_ndjson(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "b.md").write_text("# B\n\nbeta")
    (tmp_path / "docs" / "a.md").write_text("# A\n\nalpha")

    lines = build_manifest_lines(tmp_path)
    objs = [json.loads(line) for line in lines]
    assert [o["path"] for o in objs] == ["docs/a.md", "docs/b.md"]  # sorted
    assert objs[0]["title"] == "A"
    assert objs[0]["content"] == "# A\n\nalpha"
    assert len(objs[0]["sha256"]) == 64
