"""Tests for the visibility helper: coalescing, sanitisation, SQL filter."""

from __future__ import annotations

import pytest

from knowledge.visibility import (
    VISIBILITY_CRITERIA,
    effective_visibility,
    sanitize_public_body,
    strip_private_wikilinks,
)


class _StubNote:
    def __init__(self, visibility: str | None) -> None:
        self.visibility = visibility


def test_effective_visibility_public():
    assert effective_visibility(_StubNote("public")) == "public"


def test_effective_visibility_private():
    assert effective_visibility(_StubNote("private")) == "private"


def test_effective_visibility_null_defaults_private():
    assert effective_visibility(_StubNote(None)) == "private"


def test_effective_visibility_unknown_defaults_private():
    """Defensive: a leaked-through bad value still defaults to private."""
    assert effective_visibility(_StubNote("yellow")) == "private"


def test_sanitize_no_links_passthrough():
    body = "Plain text with no wikilinks."
    assert sanitize_public_body(body, private_target_ids=set()) == body


def test_sanitize_link_to_public_kept():
    body = "See [[dora-metrics]] for the four key metrics."
    assert sanitize_public_body(body, private_target_ids=set()) == body


def test_sanitize_link_to_private_stripped_to_text():
    body = "Discussed with [[Some Colleague]] yesterday."
    out = sanitize_public_body(body, private_target_ids={"some-colleague"})
    assert out == "Discussed with Some Colleague yesterday."


def test_sanitize_link_to_unresolved_gap_stripped_to_text():
    """Unresolved targets (gaps) are treated as private — no link."""
    body = "The [[Mystery Concept]] is still being researched."
    out = sanitize_public_body(body, private_target_ids={"mystery-concept"})
    assert out == "The Mystery Concept is still being researched."


def test_sanitize_multiple_links_per_line():
    body = "Both [[dora-metrics]] and [[Private Topic]] matter."
    out = sanitize_public_body(body, private_target_ids={"private-topic"})
    assert out == "Both [[dora-metrics]] and Private Topic matter."


def test_sanitize_malformed_brackets_left_alone():
    """We only strip well-formed [[X]] — partial brackets are not links."""
    body = "[ [not a link] ] and [[ok]] and [unfinished"
    out = sanitize_public_body(body, private_target_ids=set())
    assert "[[ok]]" in out
    assert "[unfinished" in out


def test_strip_private_no_links_passthrough():
    body = "Plain text with no wikilinks."
    assert strip_private_wikilinks(body, public_note_ids=set()) == body


def test_strip_private_link_to_public_kept():
    body = "See [[dora-metrics]] for the four key metrics."
    out = strip_private_wikilinks(body, public_note_ids={"dora-metrics"})
    assert out == body


def test_strip_private_link_to_private_stripped_to_text():
    body = "Discussed with [[Some Colleague]] yesterday."
    out = strip_private_wikilinks(body, public_note_ids={"dora-metrics"})
    assert out == "Discussed with Some Colleague yesterday."


def test_strip_private_dangling_target_stripped_to_text():
    """A wikilink to a non-existent note (not in the public set) is stripped."""
    body = "The [[Mystery Concept]] does not resolve to any public note."
    out = strip_private_wikilinks(body, public_note_ids={"dora-metrics"})
    assert out == "The Mystery Concept does not resolve to any public note."


def test_strip_private_multiple_links_per_line():
    body = "Both [[dora-metrics]] and [[Private Topic]] matter."
    out = strip_private_wikilinks(body, public_note_ids={"dora-metrics"})
    assert out == "Both [[dora-metrics]] and Private Topic matter."


def test_criteria_text_present():
    """Smoke check — every prompt that imports this string must keep it."""
    assert "visibility: public" in VISIBILITY_CRITERIA, (
        "criteria must teach the public visibility value"
    )
    assert "When in doubt" in VISIBILITY_CRITERIA, (
        "criteria must teach the privacy-conservative rule"
    )
    assert "private" in VISIBILITY_CRITERIA.lower(), (
        "criteria must teach the private visibility value"
    )


def test_visibility_criteria_is_re_exported_from_profile_module():
    """Drift detector: visibility.py's VISIBILITY_CRITERIA must be the
    SAME object as profile.py's, not a hand-maintained copy.

    Catches the regression where someone re-inlines the literal in
    visibility.py because they couldn't find where it lived. The shared
    object identity guarantees no drift can occur even if profile.py is
    edited; both consumers see the same updated string.
    """
    from knowledge.profile import VISIBILITY_CRITERIA as profile_criteria
    from knowledge.visibility import VISIBILITY_CRITERIA as visibility_criteria

    assert profile_criteria is visibility_criteria, (
        "visibility.py must re-export VISIBILITY_CRITERIA from "
        "knowledge.profile (not maintain a separate copy)."
    )
