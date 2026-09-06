import re
from pathlib import Path

from knowledge.tools.gen_orchestrator_bundle import (
    assemble_bundle,
    recipe_catalog_entries,
    repo_structure_digest,
)

# Path(__file__).parent is chat/ in runfiles; both the hand-written prompt and
# the generated bundle are shipped as py_test `data` (see the BUILD target),
# mirroring how chat_directives_test ships chat/directive_seed.md. Reading the
# COMMITTED artifact (rather than invoking git ls-files, which has no .git to
# walk inside the bazel test sandbox) is exactly how
# gen_repo_docs_manifest_test.py avoids testing its own git-driven path
# discovery: pure functions get unit tests, git plumbing is exercised by
# running the generator by hand.
_CHAT_DIR = Path(__file__).resolve().parent
_BASE_PROMPT = (_CHAT_DIR / "orchestrator_prompt.md").read_text(encoding="utf-8")
_COMMITTED_BUNDLE = (_CHAT_DIR / "orchestrator_bundle.md").read_text(encoding="utf-8")

# Matches ISO-ish dates/times and unix epoch runs, so a regression that starts
# stamping the bundle with wall-clock output fails a test immediately rather
# than silently breaking the provider's prefix cache.
_TIMESTAMP_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\b\d{10,13}\b")


def _write_recipe(
    tmp_path: Path, name: str, description: str | None, title: str
) -> str:
    lines = ['version: "1.0.0"', f'title: "{title}"']
    if description is not None:
        lines.append(f'description: "{description}"')
    lines.append("instructions: |")
    lines.append("  do the thing")
    (tmp_path / f"{name}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"{name}.yaml"


def test_recipe_catalog_entries_sorted_and_deterministic(tmp_path: Path):
    rel_paths = [
        _write_recipe(tmp_path, "zeta", "Zeta description", "Zeta"),
        _write_recipe(tmp_path, "alpha", "Alpha description", "Alpha"),
    ]
    first = recipe_catalog_entries(tmp_path, rel_paths)
    second = recipe_catalog_entries(tmp_path, list(reversed(rel_paths)))
    assert first == second  # order-independent: always sorted by name
    assert first == ["- alpha: Alpha description", "- zeta: Zeta description"]


def test_recipe_catalog_entries_falls_back_to_title_without_description(
    tmp_path: Path,
):
    rel_paths = [_write_recipe(tmp_path, "notitledesc", None, "Fallback Title")]
    entries = recipe_catalog_entries(tmp_path, rel_paths)
    assert entries == ["- notitledesc: Fallback Title"]


def test_repo_structure_digest_sorted():
    paths = [
        "projects/zeta/README.md",
        "projects/alpha/x.py",
    ]
    lines = repo_structure_digest(paths)
    joined = "\n".join(lines)
    assert joined.index("- alpha") < joined.index("- zeta")


def test_assemble_bundle_is_byte_deterministic():
    catalog = ["- alpha: A", "- beta: B"]
    structure = ["Top-level projects/ directories:", "- monolith"]
    first = assemble_bundle("BASE PROMPT", catalog, structure)
    second = assemble_bundle("BASE PROMPT", catalog, structure)
    assert first == second
    assert first.startswith("BASE PROMPT\n")
    assert "## Recipe catalog" in first
    assert "## Repo structure" in first
    assert first.index("## Recipe catalog") < first.index("## Repo structure")


def test_committed_bundle_contains_base_prompt():
    known_substring = "retrieval-in, text-out, no tools"
    assert known_substring in _BASE_PROMPT
    assert known_substring in _COMMITTED_BUNDLE


def test_committed_bundle_recipe_catalog_lines_sorted_and_well_formed():
    catalog_section = _COMMITTED_BUNDLE.split("## Recipe catalog", 1)[1].split(
        "## Repo structure", 1
    )[0]
    catalog_lines = [
        line for line in catalog_section.splitlines() if line.startswith("- ")
    ]
    assert catalog_lines, "expected at least one recipe catalog line"
    names = [line[2:].split(":", 1)[0] for line in catalog_lines]
    assert names == sorted(names)
    assert len(names) == len(set(names))  # one line per recipe, no duplicates


def test_committed_bundle_contains_repo_structure_digest():
    assert "## Repo structure" in _COMMITTED_BUNDLE
    assert "Top-level projects/ directories:" in _COMMITTED_BUNDLE
    assert "- monolith" in _COMMITTED_BUNDLE


def test_committed_bundle_has_no_timestamp_like_patterns():
    assert not _TIMESTAMP_LIKE.search(_COMMITTED_BUNDLE)
