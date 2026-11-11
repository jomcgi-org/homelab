"""Tests for public-body wikilink stripping."""

from __future__ import annotations

from knowledge.visibility import strip_private_wikilinks


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
