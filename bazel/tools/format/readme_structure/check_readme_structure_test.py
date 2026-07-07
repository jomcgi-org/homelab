"""Tests for check_readme_structure.py.

These use synthetic README text and a synthetic project-name list so the test
is sandbox-safe (no dependency on the real working tree, which a bazel test on
RBE cannot see). The real-tree enforcement runs as a standalone CLI in the CI
"Format check" step; here we only pin the logic contract.
"""

import pathlib

from check_readme_structure import (
    extract_repo_links,
    find_broken_links,
    find_uncovered_projects,
)

README = """# Homelab

- [Monolith](projects/monolith/): the big one
- [Sextant](projects/sextant/): code generator
- [Security](docs/security.md#threat-model): anchored link
- [GitHub](https://github.com/jomcgi/homelab): external, ignored
- Contact: [email](mailto:joe@example.com)
"""


def test_extract_ignores_external_anchor_and_mailto():
    links = extract_repo_links(README)
    assert "projects/monolith/" in links
    assert "docs/security.md" in links  # #threat-model stripped
    assert not any(link.startswith(("http", "mailto")) for link in links)
    assert "#threat-model" not in "".join(links)


def test_broken_link_detected(tmp_path: pathlib.Path):
    (tmp_path / "projects" / "monolith").mkdir(parents=True)
    (tmp_path / "projects" / "sextant").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "security.md").write_text("x")
    # README links to projects/sextant/, which exists, and projects/monolith/,
    # which exists, but we delete sextant to simulate a removed project.
    (tmp_path / "projects" / "sextant").rmdir()

    broken = find_broken_links(tmp_path, "README.md", README)
    assert broken == ["projects/sextant/"]


def test_no_broken_links_when_all_present(tmp_path: pathlib.Path):
    for d in ("projects/monolith", "projects/sextant", "docs"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "docs" / "security.md").write_text("x")
    assert find_broken_links(tmp_path, "README.md", README) == []


def test_uncovered_project_detected():
    dirs = ["monolith", "sextant", "brand_new_thing"]
    uncovered = find_uncovered_projects(dirs, README, allowlist=frozenset())
    assert uncovered == ["brand_new_thing"]


def test_allowlisted_project_is_covered():
    dirs = ["monolith", "brand_new_thing"]
    uncovered = find_uncovered_projects(
        dirs, README, allowlist=frozenset({"brand_new_thing"})
    )
    assert uncovered == []


def test_mention_requires_projects_path_not_bare_name():
    # The README talks about "sextant" in prose but never links projects/sextant.
    readme = "# Homelab\n\nWe use sextant to generate code.\n"
    uncovered = find_uncovered_projects(["sextant"], readme, allowlist=frozenset())
    assert uncovered == ["sextant"]
