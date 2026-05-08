"""Tests for the visibility helper: coalescing, sanitisation, SQL filter."""

from __future__ import annotations

import pytest

from knowledge.visibility import (
    VISIBILITY_CRITERIA,
    effective_visibility,
    sanitize_public_body,
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


def test_criteria_text_present():
    """Smoke check — every prompt that imports this string must keep it."""
    assert "visibility: public" in VISIBILITY_CRITERIA
    assert "When in doubt: `private`." in VISIBILITY_CRITERIA
