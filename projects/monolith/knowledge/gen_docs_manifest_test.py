import json
from pathlib import Path

from knowledge.tools.gen_docs_manifest import (
    _should_index,
    build_manifest,
    category_for,
    derive_title,
    make_slug,
    section_for,
)


def test_derive_title_prefers_h1():
    assert derive_title("# Hello\n\nbody", "docs/x.md") == "Hello"


def test_derive_title_falls_back_to_basename():
    assert derive_title("no heading", "docs/decisions/agents/001-x.md") == "001-x"


def test_should_index_allowlist():
    # Included: top-level reference docs and the whole ADR tree (incl. index).
    assert _should_index("docs/security.md")
    assert _should_index("docs/event-bus.md")
    assert _should_index("docs/decisions/index.md")
    assert _should_index("docs/decisions/agents/001-background-agents.md")
    assert _should_index("docs/decisions/docs/002-x.md")
    # Excluded: plans, nested non-ADR docs, non-docs paths, wrong extension,
    # the personal blocklist, and the manifest itself.
    assert not _should_index("docs/plans/2026-06-19-x.md")
    assert not _should_index("docs/WORKING-WITH-JOE.md")
    assert not _should_index("docs/security.txt")
    assert not _should_index("projects/monolith/README.md")
    assert not _should_index("README.md")
    assert not _should_index(
        "projects/monolith/frontend/src/lib/public/docs/docs-manifest.json"
    )


def test_make_slug():
    assert make_slug("docs/security.md") == "security"
    assert make_slug("docs/decisions/index.md") == "decisions"
    assert (
        make_slug("docs/decisions/agents/001-background-agents.md")
        == "decisions/agents/001-background-agents"
    )


def test_section_and_category():
    assert section_for("docs/security.md") == "Reference"
    assert section_for("docs/decisions/agents/001-x.md") == "Decisions"
    assert category_for("docs/decisions/agents/001-x.md") == "agents"
    assert category_for("docs/decisions/index.md") == ""
    assert category_for("docs/security.md") == ""


def test_build_manifest_ordering_and_shape(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "services.md").write_text("# Services\n\nbody")
    (tmp_path / "docs" / "agents.md").write_text("# Agents\n\nbody")
    dec = tmp_path / "docs" / "decisions"
    (dec / "agents").mkdir(parents=True)
    (dec / "platform").mkdir(parents=True)
    (dec / "index.md").write_text("# ADRs\n\nindex")
    (dec / "agents" / "002-b.md").write_text("# Two\n\nb")
    (dec / "agents" / "001-a.md").write_text("# One\n\na")
    (dec / "platform" / "001-p.md").write_text("# P\n\np")

    paths = [
        "docs/services.md",
        "docs/agents.md",
        "docs/decisions/index.md",
        "docs/decisions/agents/002-b.md",
        "docs/decisions/agents/001-a.md",
        "docs/decisions/platform/001-p.md",
    ]
    entries = build_manifest(tmp_path, paths)

    # Reference docs (alphabetical) first, then the decisions index, then ADRs
    # grouped by category (alpha) and ordered by numeric prefix.
    assert [e["slug"] for e in entries] == [
        "agents",
        "services",
        "decisions",
        "decisions/agents/001-a",
        "decisions/agents/002-b",
        "decisions/platform/001-p",
    ]
    # order is the stable entry index.
    assert [e["order"] for e in entries] == [0, 1, 2, 3, 4, 5]
    # Shape + section grouping.
    assert entries[0]["section"] == "Reference"
    assert entries[2]["section"] == "Decisions"
    assert entries[0]["title"] == "Agents"
    assert entries[0]["content"] == "# Agents\n\nbody"
    assert set(entries[0]) == {"path", "slug", "title", "section", "order", "content"}


def test_build_manifest_strips_nul_bytes(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "n.md").write_text("# N\n\nbe\x00fore")
    entry = build_manifest(tmp_path, ["docs/n.md"])[0]
    assert "\x00" not in entry["content"]
    assert entry["content"] == "# N\n\nbefore"


def test_manifest_json_round_trips(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A\n\nalpha")
    entries = build_manifest(tmp_path, ["docs/a.md"])
    assert json.loads(json.dumps(entries)) == entries
