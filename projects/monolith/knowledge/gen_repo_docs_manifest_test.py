import json
from pathlib import Path

from knowledge.tools.gen_repo_docs_manifest import (
    _should_index,
    build_manifest_lines,
    derive_title,
)


def test_derive_title_prefers_h1():
    assert derive_title("# Hello\n\nbody", "docs/x.md") == "Hello"


def test_derive_title_falls_back_to_path():
    assert derive_title("no heading here", "docs/x.md") == "docs/x.md"


def test_should_index_includes_and_excludes():
    # Included: *.md under docs/ or projects/, and any CLAUDE.md.
    assert _should_index("docs/a.md")
    assert _should_index("docs/architecture-notes/004.md")
    assert _should_index("projects/svc/README.md")
    assert _should_index("CLAUDE.md")
    assert _should_index(".claude/CLAUDE.md")
    assert _should_index("projects/monolith/CLAUDE.md")
    assert _should_index("bazel/ARCHITECTURE.md")
    assert _should_index("bazel/ocaml/README.md")
    # Excluded: wrong extension, outside the indexed dirs, or a noise segment.
    assert not _should_index("projects/svc/notes.txt")
    assert not _should_index("README.md")  # repo-root, not under docs/ or projects/
    assert not _should_index("src/x.md")
    assert not _should_index("projects/svc/node_modules/z.md")
    assert not _should_index("projects/svc/frontend/build/g.md")
    assert not _should_index("bazel/ocaml/third_party/fmt/LICENSE.md")
    assert not _should_index("bazel/semgrep/tests/fixtures/no-stale-repo-paths.md")
    # The generated manifest never indexes itself.
    assert not _should_index("projects/monolith/knowledge/repo_docs_manifest.ndjson")


def test_build_manifest_lines_sorted_ndjson(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "b.md").write_text("# B\n\nbeta")
    (tmp_path / "docs" / "a.md").write_text("# A\n\nalpha")

    # build_manifest_lines takes an explicit path list (discovery is git-driven
    # and tested separately), and sorts for a stable, diff-friendly manifest.
    lines = build_manifest_lines(tmp_path, ["docs/b.md", "docs/a.md"])
    objs = [json.loads(line) for line in lines]
    assert [o["path"] for o in objs] == ["docs/a.md", "docs/b.md"]  # sorted
    assert objs[0]["title"] == "A"
    assert objs[0]["content"] == "# A\n\nalpha"
    assert len(objs[0]["sha256"]) == 64


def test_build_manifest_lines_strips_nul_bytes(tmp_path: Path):
    # A doc with a NUL byte must not reach the manifest: Postgres TEXT columns
    # reject 0x00 and the reconcile insert would fail.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "n.md").write_text("# N\n\nbe\x00fore")

    obj = json.loads(build_manifest_lines(tmp_path, ["docs/n.md"])[0])
    assert "\x00" not in obj["content"]
    assert obj["content"] == "# N\n\nbefore"
