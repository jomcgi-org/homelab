import json

import pytest  # noqa: F401

from bench.verifiers import get_verifier

BUGS = [
    {"id": "order", "file": "pkg/purge.py", "lines": [10, 14]},
    {"id": "window", "file": "pkg/purge.py", "lines": [30, 30]},
]


def _args(**over):
    args = {"bugs": BUGS, "tolerance": 2, "fp_penalty": 0.25, "pass_threshold": 0.8}
    args.update(over)
    return args


def _review(tmp_path, findings):
    (tmp_path / "review.json").write_text(json.dumps(findings))
    return get_verifier("review-findings")(tmp_path, _args())


def test_all_bugs_found_scores_one(tmp_path):
    r = _review(
        tmp_path,
        [
            {"file": "pkg/purge.py", "line": 12, "description": "x"},
            {"file": "pkg/purge.py", "line": 31, "description": "y"},
        ],
    )
    assert r.score == 1.0 and r.passed


def test_tolerance_widens_the_bug_range(tmp_path):
    r = _review(tmp_path, [{"file": "pkg/purge.py", "line": 32, "description": ""}])
    assert r.score == 0.5 and "missed: order" in r.feedback


def test_false_positives_cost_points(tmp_path):
    r = _review(
        tmp_path,
        [
            {"file": "pkg/purge.py", "line": 12},
            {"file": "pkg/purge.py", "line": 30},
            {"file": "pkg/other.py", "line": 12},
            {"file": "pkg/purge.py", "line": 50},
        ],
    )
    assert r.score == pytest.approx((2 - 0.5) / 2)
    assert "2 false positive(s)" in r.feedback


def test_duplicate_finding_neither_scores_nor_costs(tmp_path):
    r = _review(
        tmp_path,
        [{"file": "pkg/purge.py", "line": 10}, {"file": "pkg/purge.py", "line": 13}],
    )
    assert r.score == 0.5 and "0 false positive(s)" in r.feedback


def test_repo_and_diff_prefixed_paths_match(tmp_path):
    r = _review(
        tmp_path,
        [
            {"file": "projects/monolith/pkg/purge.py", "line": "L11"},
            {"file": "b/pkg/purge.py", "line": "30"},
        ],
    )
    assert r.score == 1.0


def test_spraying_every_line_scores_zero(tmp_path):
    r = _review(tmp_path, [{"file": "pkg/purge.py", "line": i} for i in range(200)])
    assert r.score == 0.0


def test_findings_object_form_is_accepted(tmp_path):
    r = _review(tmp_path, {"findings": [{"file": "pkg/purge.py", "line": 30}]})
    assert r.score == 0.5


def test_missing_or_malformed_review_scores_zero(tmp_path):
    v = get_verifier("review-findings")
    assert v(tmp_path, _args()).score == 0.0
    (tmp_path / "review.json").write_text("not json")
    r = v(tmp_path, _args())
    assert r.score == 0.0 and "not valid JSON" in r.feedback
    (tmp_path / "review.json").write_text('"a string"')
    assert v(tmp_path, _args()).score == 0.0
