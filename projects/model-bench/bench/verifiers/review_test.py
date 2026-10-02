import json

import pytest

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


DECOY_ARGS = {
    "bugs": [
        {"id": "fk", "file": "pkg/purge.py", "lines": [10, 12]},
        {"id": "offset", "file": "pkg/purge.py", "lines": [[30, 30], [60, 60]]},
    ],
    "decoys": [{"id": "cascade", "file": "pkg/purge.py", "lines": [5, 8]}],
    "tolerance": 2,
    "fp_penalty": 0.25,
    "decoy_penalty": 0.5,
}


def _grade(tmp_path, findings, args=DECOY_ARGS):
    (tmp_path / "review.json").write_text(json.dumps(findings))
    return get_verifier("review-findings")(tmp_path, args)


def test_decoy_hit_costs_decoy_penalty_and_is_reported(tmp_path):
    r = _grade(
        tmp_path,
        [{"file": "pkg/purge.py", "line": 11}, {"file": "pkg/purge.py", "line": 6}],
    )
    assert r.score == pytest.approx((1 - 0.5) / 2)
    assert "1 decoy hit(s) (cascade)" in r.feedback


def test_nearest_target_owns_a_finding_between_decoy_and_bug(tmp_path):
    # Line 9 is 1 from the bug and 1 from the decoy; the bug wins the tie, and
    # line 8 sits inside the decoy.
    r = _grade(tmp_path, [{"file": "pkg/purge.py", "line": 9}])
    assert r.score == 0.5 and "0 decoy hit(s)" in r.feedback
    r = _grade(tmp_path, [{"file": "pkg/purge.py", "line": 8}])
    assert r.score == 0.0 and "(cascade)" in r.feedback


def test_multi_range_bug_matches_at_either_site(tmp_path):
    for line in (30, 61):
        r = _grade(tmp_path, [{"file": "pkg/purge.py", "line": line}])
        assert r.score == 0.5 and "missed: fk" in r.feedback


def test_f2_scoring_weights_recall_over_precision(tmp_path):
    (tmp_path / "review.json").write_text(
        json.dumps(
            [
                {"file": "pkg/purge.py", "line": 12, "description": "x"},
                {"file": "pkg/purge.py", "line": 50, "description": "noise"},
            ]
        )
    )
    r = get_verifier("review-findings")(tmp_path, _args(scoring="f2"))
    # recall 1/2, precision 1/2: F2 = 5 * 0.25 / (2 + 0.5) = 0.5
    assert r.score == pytest.approx(0.5)


def test_f2_scoring_counts_decoys_double_by_default(tmp_path):
    (tmp_path / "review.json").write_text(
        json.dumps(
            [
                {"file": "pkg/purge.py", "line": 12, "description": "x"},
                {"file": "pkg/purge.py", "line": 30, "description": "y"},
                {"file": "pkg/purge.py", "line": 60, "description": "decoy"},
            ]
        )
    )
    decoys = [{"id": "d", "file": "pkg/purge.py", "lines": [60, 60]}]
    r = get_verifier("review-findings")(tmp_path, _args(scoring="f2", decoys=decoys))
    # recall 1, precision 2 / (2 + 2): F2 = 5 * 0.5 / (2 + 1) = 0.8333
    assert r.score == pytest.approx(5 * 0.5 / 3)


def test_unknown_scoring_is_rejected(tmp_path):
    (tmp_path / "review.json").write_text("[]")
    with pytest.raises(ValueError, match="scoring"):
        get_verifier("review-findings")(tmp_path, _args(scoring="vibes"))
