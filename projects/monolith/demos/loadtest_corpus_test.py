"""Tests for the load-test corpus loader.

Covers the shape and size bounds the load-test harness (Task 5) relies on:
five semgrep-pack entries with plausible file sizes and extensions, a handful
of varied sandbox scripts, and a clear error for an unknown workload.
"""

from __future__ import annotations

import pytest

from demos.loadtest_corpus import load_corpus

_EXPECTED_SEMGREP_NAMES = {"python", "golang", "javascript", "kubernetes", "rust"}
_EXPECTED_EXTENSIONS = {"py", "go", "ts", "yaml", "rs"}


def test_semgrep_corpus_covers_all_five_packs():
    entries = load_corpus("semgrep")
    names = {e["name"] for e in entries}
    assert names == _EXPECTED_SEMGREP_NAMES

    for entry in entries:
        # Lower bound is deliberately loose: samples are sized to exercise the
        # Pro rules, not to pad scan time (python was trimmed 179 -> ~80 lines
        # because scan wall time scales with file size).
        line_count = len(entry["content"].splitlines())
        assert 40 <= line_count <= 400, (entry["name"], line_count)

        extension = entry["path"].rsplit(".", 1)[-1]
        assert extension in _EXPECTED_EXTENSIONS, (entry["name"], entry["path"])


def test_sandbox_corpus_present_and_bounded():
    entries = load_corpus("sandbox")
    assert 5 <= len(entries) <= 8

    for entry in entries:
        assert entry["content"].strip()
        line_count = len(entry["content"].splitlines())
        assert 30 <= line_count <= 200, (entry["name"], line_count)


def test_unknown_workload_raises():
    with pytest.raises(ValueError):
        load_corpus("not-a-real-workload")
