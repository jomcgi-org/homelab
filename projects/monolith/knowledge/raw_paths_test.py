"""Tests for content-addressed raw input IDs."""

from knowledge.raw_paths import compute_raw_id


def test_compute_raw_id_is_sha256_of_bytes():
    content = "# Hello\n\nBody."
    expected = "39c483b62cff864432b5302bd48b97d716d5713a9f53581167a17507086a301e"
    assert compute_raw_id(content) == expected


def test_compute_raw_id_is_stable_across_calls():
    content = "foo"
    assert compute_raw_id(content) == compute_raw_id(content)
