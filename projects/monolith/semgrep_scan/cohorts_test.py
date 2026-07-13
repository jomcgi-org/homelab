"""Unit tests for semgrep_scan.cohorts.diff_cohort (pure, no I/O)."""

import pytest

from semgrep_scan.cohorts import diff_cohort, language_for


def _entry(filename, additions=0, deletions=0, status="modified"):
    return {
        "filename": filename,
        "additions": additions,
        "deletions": deletions,
        "status": status,
    }


def test_language_for_maps_extensions():
    assert language_for("app/main.py") == "python"
    assert language_for("svc/h.go") == "go"
    assert language_for("web/c.jsx") == "javascript"
    assert language_for("web/c.js") == "javascript"
    assert language_for("web/c.tsx") == "typescript"
    assert language_for("web/c.ts") == "typescript"
    assert language_for("core/lib.rs") == "rust"
    assert language_for("README.md") is None


def test_diff_cohort_counts_scannable_changed_lines_by_language():
    entries = [
        _entry("app/main.py", additions=10, deletions=2),
        _entry("app/util.py", additions=3, deletions=0),
        _entry("svc/handler.go", additions=5, deletions=5),
        _entry("README.md", additions=100, deletions=100),  # unscannable -> skip
        _entry("old/gone.py", status="removed"),  # removed -> skip
    ]
    cohort = diff_cohort(entries)
    assert cohort["file_count"] == 3
    assert cohort["changed_lines"] == 25  # 12 + 3 + 10
    assert cohort["languages"] == {"python": 15, "go": 10}


def test_diff_cohort_empty():
    assert diff_cohort([]) == {
        "file_count": 0,
        "changed_lines": 0,
        "languages": {},
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
