"""Tests for removing generated Links sections before indexing."""

import pytest

from knowledge.wikilinks import strip_links_section


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", ""),
        ("Authored [[link]].\n", "Authored [[link]].\n"),
        ("Body\n\n## Links\n\nUp: [[generated]]\n", "Body\n"),
        ("Body\n## Links\nGenerated\n\n## Later\nMore\n", "Body"),
        ("Body\n### Links\nKeep this.\n", "Body\n### Links\nKeep this.\n"),
    ],
)
def test_strip_links_section(body, expected):
    assert strip_links_section(body) == expected
    assert strip_links_section(expected) == expected
