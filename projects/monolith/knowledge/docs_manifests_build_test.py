"""The doc manifests are build outputs (#6446): check the genrules against the
generators and check the runtime loader finds the build output.

The test's `data` carries both genrule outputs and the doc files they were
built from, so the runfiles tree mirrors the repo for those paths. Regenerating
from the runfiles copies and comparing bytes proves the genrule ran the
generator over the docs, with the same selection and serialization a local
``python3 gen_*.py`` run applies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from knowledge import repo_docs
from knowledge.tools import gen_docs_manifest, gen_repo_docs_manifest

REPO_MANIFEST_REL = "projects/monolith/knowledge/repo_docs_manifest.ndjson"
PUBLIC_MANIFEST_REL = gen_docs_manifest.MANIFEST_REL


def _runfiles_root() -> Path:
    srcdir = os.environ.get("TEST_SRCDIR")
    if not srcdir:
        pytest.skip("needs the Bazel runfiles tree (run via bazel test)")
    return Path(srcdir) / os.environ.get("TEST_WORKSPACE", "_main")


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _runfiles_root()
    monkeypatch.chdir(root)
    monkeypatch.delenv("BUILD_WORKSPACE_DIRECTORY", raising=False)
    monkeypatch.delenv("REPO_DOCS_MANIFEST_PATH", raising=False)
    return root


def _built_repo_paths(root: Path) -> list[str]:
    text = (root / REPO_MANIFEST_REL).read_text(encoding="utf-8")
    return [json.loads(line)["path"] for line in text.splitlines() if line]


def test_repo_docs_manifest_matches_generator(root: Path, tmp_path: Path):
    paths = _built_repo_paths(root)
    # Anchors from each filegroup shape: the root package's explicit globs, a
    # package-wide glob, and the monolith package's own glob.
    for anchor in (
        "AGENTS.md",
        "bazel/ARCHITECTURE.md",
        "docs/writing.md",
        "projects/platform/ARCHITECTURE.md",
        "projects/monolith/ARCHITECTURE.md",
    ):
        assert anchor in paths
    assert paths == gen_repo_docs_manifest.select_doc_paths(paths)

    out = tmp_path / "repo_docs_manifest.ndjson"
    assert gen_repo_docs_manifest.main(["--out", str(out), *paths]) == 0
    assert (root / REPO_MANIFEST_REL).read_bytes() == out.read_bytes()


def test_public_docs_manifest_matches_generator(root: Path, tmp_path: Path):
    candidates = _built_repo_paths(root)
    built = json.loads((root / PUBLIC_MANIFEST_REL).read_text(encoding="utf-8"))
    built_paths = [entry["path"] for entry in built]
    for _project, directory in gen_docs_manifest.PUBLIC_PROJECTS:
        assert f"{directory}/README.md" in built_paths

    out = tmp_path / "docs-manifest.json"
    assert gen_docs_manifest.main(["--out", str(out), *candidates]) == 0
    assert (root / PUBLIC_MANIFEST_REL).read_bytes() == out.read_bytes()


def test_manifest_path_finds_build_output_in_runfiles(root: Path):
    path = repo_docs.manifest_path()
    assert path.is_file(), path
    assert path.read_bytes() == (root / REPO_MANIFEST_REL).read_bytes()
    entries = repo_docs.load_manifest()
    assert [entry.path for entry in entries] == _built_repo_paths(root)
