"""Unit tests for the retired ADR path guard.

Synthetic inputs only: a test that breaks when an unrelated ADR is deleted
would defeat the purpose of the guard it is testing.
"""

from __future__ import annotations

import check_doc_links as c

PREFIX = c._DECISIONS_PREFIX
REAL = f"{PREFIX}agents/020-deprecate-context-forge-mcp-gateway.md"
GONE = f"{PREFIX}agents/003-context-forge.md"


def test_finds_a_reference():
    assert c.find_refs(f"see {REAL} for why") == [REAL]


def test_deduplicates_and_keeps_order():
    text = f"{REAL} then {GONE} then {REAL} again"
    assert c.find_refs(text) == [REAL, GONE]


def test_ignores_a_bare_category_path():
    assert c.find_refs(f"everything under {PREFIX}agents/ is rationale") == []


def test_ignores_an_unnumbered_file():
    assert c.find_refs(f"{PREFIX}embervm/README.md") == []


def test_reference_is_rejected():
    out = c.check_file("projects/x/main.go", f"// {REAL}")
    assert len(out) == 1
    assert REAL in out[0]


def test_retired_reference_is_a_violation():
    out = c.check_file("projects/x/main.go", f"// {GONE}")
    assert len(out) == 1
    assert GONE in out[0]
    assert "ARCHITECTURE.md" in out[0]


def test_reports_every_missing_reference_in_one_file():
    text = f"{GONE} and {PREFIX}agents/005-role-based-mcp-access.md"
    assert len(c.check_file("a/b.md", text)) == 2


def test_test_files_are_skipped():
    for path in (
        "projects/x/thing_test.py",
        "projects/x/thing.test.js",
        "projects/x/thing.test.ts",
        "projects/x/thing_test.go",
        "projects/x/thing_test.sh",
    ):
        assert not c.should_scan(path), path
        assert c.check_file(path, f"{GONE}") == []


def test_generated_manifests_are_skipped():
    path = "projects/monolith/knowledge/repo_docs_manifest.ndjson"
    assert not c.should_scan(path)
    assert c.check_file(path, f"{GONE}") == []


def test_ordinary_source_is_scanned():
    for path in (
        "projects/x/main.go",
        "projects/x/BUILD",
        "docs/runbooks/a.md",
        "projects/monolith/knowledge/tools/gen_docs_manifest.py",
    ):
        assert c.should_scan(path), path


def test_binary_suffixes_are_skipped():
    assert not c.should_scan("docs/img/diagram.png")
    assert not c.should_scan("charts/thing-1.0.0.tgz")


def test_an_adr_referencing_a_sibling_is_caught():
    out = c.check_file(
        f"{PREFIX}agents/020-deprecate-context-forge-mcp-gateway.md",
        "**Supersedes:** [003](003-context-forge.md)",
    )
    assert len(out) == 1
    assert GONE in out[0]


def test_relative_sibling_link_resolves_against_the_adr_directory():
    text = "[020](020-deprecate-context-forge-mcp-gateway.md)"
    assert c.find_relative_links(f"{PREFIX}agents/003-x.md", text) == [REAL]


def test_relative_link_across_categories_resolves():
    text = "[041](../agents/041-hot-git-mirror-agent-workspaces.md)"
    assert c.find_relative_links(f"{PREFIX}tooling/011-x.md", text) == [
        f"{PREFIX}agents/041-hot-git-mirror-agent-workspaces.md"
    ]


def test_relative_links_outside_the_adr_tree_are_out_of_scope():
    """Links to code and to the retired docs/plans/ tree are pre-existing rot."""
    text = (
        "[plan](../../plans/2026-05-07-thing.md) "
        "[code](../../../projects/agent_platform/README.md)"
    )
    assert c.find_relative_links(f"{PREFIX}agents/042-x.md", text) == []


def test_links_inside_a_fenced_block_are_illustrations_not_references():
    text = "\n".join(
        [
            "real: [020](020-deprecate-context-forge-mcp-gateway.md)",
            "```markdown",
            "See the [Ships API](../services/ships_api/README.md) for details.",
            "[gone](003-context-forge.md)",
            "```",
        ]
    )
    assert c.find_relative_links(f"{PREFIX}agents/999-x.md", text) == [REAL]


def test_relative_links_only_apply_inside_the_adr_tree():
    text = "[a](003-context-forge.md)"
    assert c.find_relative_links("projects/mcp/README.md", text) == []


def test_anchor_suffix_is_stripped():
    text = "[020](020-deprecate-context-forge-mcp-gateway.md#decision)"
    assert c.find_relative_links(f"{PREFIX}agents/003-x.md", text) == [REAL]
