import re
from pathlib import Path

from knowledge.tools.gen_orchestrator_bundle import (
    assemble_bundle,
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


def test_repo_structure_digest_sorted():
    paths = [
        "projects/zeta/README.md",
        "projects/alpha/x.py",
    ]
    lines = repo_structure_digest(paths)
    joined = "\n".join(lines)
    assert joined.index("- alpha") < joined.index("- zeta")


def test_assemble_bundle_is_byte_deterministic():
    structure = ["Top-level projects/ directories:", "- monolith"]
    first = assemble_bundle("BASE PROMPT", structure)
    second = assemble_bundle("BASE PROMPT", structure)
    assert first == second
    assert first.startswith("BASE PROMPT\n")
    assert "## Recipe catalog" not in first
    assert "## Repo structure" in first


def test_committed_bundle_contains_base_prompt():
    known_substring = "retrieval-in, text-out, no tools"
    assert known_substring in _BASE_PROMPT
    assert known_substring in _COMMITTED_BUNDLE


def test_committed_bundle_matches_prompt_and_has_no_recipe_section():
    structure = _COMMITTED_BUNDLE.split("\n## Repo structure\n\n", 1)[1]
    assert _COMMITTED_BUNDLE == assemble_bundle(
        _BASE_PROMPT.rstrip("\n"), structure.rstrip("\n").splitlines()
    )
    assert "## Recipe catalog" not in _COMMITTED_BUNDLE


def test_committed_bundle_contains_repo_structure_digest():
    assert "## Repo structure" in _COMMITTED_BUNDLE
    assert "Top-level projects/ directories:" in _COMMITTED_BUNDLE
    assert "- monolith" in _COMMITTED_BUNDLE


def test_committed_bundle_has_no_timestamp_like_patterns():
    assert not _TIMESTAMP_LIKE.search(_COMMITTED_BUNDLE)
