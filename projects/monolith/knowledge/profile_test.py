"""Tests for knowledge.profile: structure invariants + load-bearing values."""

from __future__ import annotations

from datetime import date

from knowledge.profile import (
    ASYMMETRIC_ERROR_PREFERENCE,
    IDENTITY,
    PRIVATE_CATEGORIES,
    PROFILE_UPDATED,
    PROFILE_VERSION,
    RELEVANCE_KEEP,
    RELEVANCE_SKIP,
    VISIBILITY_CRITERIA,
)


def test_profile_version_is_string_and_present():
    """Version pin must exist and be a non-empty string (consumers can pin
    against this, refusing stale rubrics if they re-pin against an older
    version)."""
    assert isinstance(PROFILE_VERSION, str)
    assert PROFILE_VERSION


def test_profile_updated_is_iso_date():
    """PROFILE_UPDATED must be a parseable YYYY-MM-DD string so consumers
    can age-check the rubric."""
    parsed = date.fromisoformat(PROFILE_UPDATED)
    assert parsed.year >= 2026


def test_relevance_keep_rows_have_required_keys():
    """Every relevance-keep row must have 'domain' + 'signals' so the
    structure stays iterable for future classifier code."""
    for i, row in enumerate(RELEVANCE_KEEP):
        assert "domain" in row, f"row {i} missing 'domain'"
        assert "signals" in row, f"row {i} missing 'signals'"
        assert row["domain"], f"row {i} 'domain' is empty"
        assert row["signals"], f"row {i} 'signals' is empty"


def test_relevance_skip_rows_have_required_keys():
    """Every relevance-skip row must have 'category' + 'examples'."""
    for i, row in enumerate(RELEVANCE_SKIP):
        assert "category" in row, f"row {i} missing 'category'"
        assert "examples" in row, f"row {i} missing 'examples'"


def test_private_categories_have_name_and_seeds():
    """Every private category must declare a name plus a non-empty seeds list."""
    for i, cat in enumerate(PRIVATE_CATEGORIES):
        assert "name" in cat and cat["name"], f"category {i} missing/empty 'name'"
        assert "seeds" in cat, f"category {i} missing 'seeds'"
        seeds = cat["seeds"]
        assert isinstance(seeds, list), f"category {i} seeds must be a list"
        assert seeds, f"category {i} 'seeds' is empty"


def test_visibility_criteria_contains_load_bearing_phrases():
    """Drift detector: VISIBILITY_CRITERIA is inlined into the gardener
    distill prompts (gardener.py:161, 197). These specific phrases must
    survive any refactor of the criteria text -- if any disappears, the
    gardener LLM loses guardrails."""
    assert "visibility: public" in VISIBILITY_CRITERIA
    assert "visibility: private" in VISIBILITY_CRITERIA
    assert "When in doubt" in VISIBILITY_CRITERIA


def test_identity_mentions_career_thesis():
    """Identity must contain the career-thesis phrase so future relevance
    classifiers can tie-break on it ('does this atom serve the
    remove-complexity-for-other-engineers frame?')."""
    assert "remove complexity for other engineers" in IDENTITY.lower()


def test_asymmetric_error_preference_is_private():
    """Profile establishes that 'when in doubt, private' is the binding
    asymmetric-error rule. Pinned here so a refactor that flips it to
    'public' is loud."""
    assert ASYMMETRIC_ERROR_PREFERENCE == "private"
