"""Tests for perf_compare.build_comparisons.

All inputs are hand-built literals with fixed timezone-aware datetimes; no
clock/random calls, so tests are fully deterministic.
"""

from __future__ import annotations

import datetime

import pytest

from semgrep_scan.perf_compare import build_comparisons

UTC = datetime.timezone.utc


def _dt(hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 7, 11, hour, minute, tzinfo=UTC)


def _row(
    scan_id: int,
    is_full_scan: bool = False,
    branch: str = "feature-x",
    scan_ref: str = "refs/pull/1/merge",
    commit_sha: str = "aaa111",
    total_time: float = 10.0,
    findings_total: int = 3,
    scan_completed_at: datetime.datetime | None = None,
) -> dict:
    return {
        "scan_id": scan_id,
        "is_full_scan": is_full_scan,
        "branch": branch,
        "scan_ref": scan_ref,
        "commit_sha": commit_sha,
        "total_time": total_time,
        "findings_total": findings_total,
        "scan_completed_at": scan_completed_at,
    }


def test_commit_match_computes_speedup():
    rb = _row(1, commit_sha="sha1", total_time=10.0, scan_completed_at=_dt(10))
    sms = _row(2, commit_sha="sha1", total_time=40.0, scan_completed_at=_dt(10, 5))

    result = build_comparisons([rb], [sms])

    assert len(result) == 1
    row = result[0]
    assert row["match_kind"] == "commit"
    assert row["commit_sha"] == "sha1"
    assert row["route_b"]["scan_id"] == 1
    assert row["sms"]["scan_id"] == 2
    assert row["speedup"] == pytest.approx(4.0)


def test_full_vs_pr_never_cross_match_same_commit():
    rb = _row(
        1,
        is_full_scan=True,
        branch="main",
        commit_sha="sha-shared",
        scan_completed_at=_dt(9),
    )
    sms = _row(
        2, is_full_scan=False, commit_sha="sha-shared", scan_completed_at=_dt(9, 1)
    )

    result = build_comparisons([rb], [sms])

    assert len(result) == 2
    kinds = {row["match_kind"] for row in result}
    assert kinds == {"one-sided"}
    sides = [(row["route_b"], row["sms"]) for row in result]
    # one row has only route_b populated, the other only sms
    assert any(rb_side is not None and sms_side is None for rb_side, sms_side in sides)
    assert any(rb_side is None and sms_side is not None for rb_side, sms_side in sides)


def test_ref_match_when_commits_differ():
    rb = _row(
        1, commit_sha="sha-rb", scan_ref="refs/pull/7/merge", scan_completed_at=_dt(8)
    )
    sms = _row(
        2,
        commit_sha="sha-sms",
        scan_ref="refs/pull/7/merge",
        scan_completed_at=_dt(8, 2),
    )

    result = build_comparisons([rb], [sms])

    assert len(result) == 1
    row = result[0]
    assert row["match_kind"] == "ref"
    assert row["scan_ref"] == "refs/pull/7/merge"
    assert row["route_b"]["scan_id"] == 1
    assert row["sms"]["scan_id"] == 2


def test_date_match_for_main_full_scans_with_no_common_commit_or_ref():
    rb = _row(
        1,
        is_full_scan=True,
        branch="main",
        commit_sha="sha-rb-only",
        scan_ref="ref-rb-only",
        scan_completed_at=_dt(12),
    )
    sms_far = _row(
        2,
        is_full_scan=True,
        branch="main",
        commit_sha="sha-sms-far",
        scan_ref="ref-sms-far",
        scan_completed_at=_dt(4),
    )
    sms_near = _row(
        3,
        is_full_scan=True,
        branch="main",
        commit_sha="sha-sms-near",
        scan_ref="ref-sms-near",
        scan_completed_at=_dt(12, 10),
    )

    result = build_comparisons([rb], [sms_far, sms_near])

    dated_matches = [row for row in result if row["match_kind"] == "date"]
    assert len(dated_matches) == 1
    matched = dated_matches[0]
    assert matched["route_b"]["scan_id"] == 1
    assert matched["sms"]["scan_id"] == 3  # nearer in time than sms_far

    # the unmatched sms row is emitted one-sided
    one_sided = [row for row in result if row["match_kind"] == "one-sided"]
    assert len(one_sided) == 1
    assert one_sided[0]["sms"]["scan_id"] == 2


def test_date_match_does_not_apply_to_pr_scans():
    rb = _row(
        1,
        is_full_scan=False,
        branch="feature-x",
        commit_sha="a",
        scan_ref="ref-a",
        scan_completed_at=_dt(9),
    )
    sms = _row(
        2,
        is_full_scan=False,
        branch="feature-y",
        commit_sha="b",
        scan_ref="ref-b",
        scan_completed_at=_dt(9, 1),
    )

    result = build_comparisons([rb], [sms])

    assert all(row["match_kind"] == "one-sided" for row in result)


def test_one_sided_route_b_only():
    rb = _row(1, commit_sha="lonely-rb", scan_completed_at=_dt(6))

    result = build_comparisons([rb], [])

    assert len(result) == 1
    row = result[0]
    assert row["match_kind"] == "one-sided"
    assert row["route_b"]["scan_id"] == 1
    assert row["sms"] is None
    assert row["speedup"] is None
    assert row["commit_sha"] == "lonely-rb"


def test_one_sided_sms_only():
    sms = _row(2, commit_sha="lonely-sms", scan_completed_at=_dt(6))

    result = build_comparisons([], [sms])

    assert len(result) == 1
    row = result[0]
    assert row["match_kind"] == "one-sided"
    assert row["sms"]["scan_id"] == 2
    assert row["route_b"] is None
    assert row["speedup"] is None
    assert row["commit_sha"] == "lonely-sms"


def test_speedup_none_when_route_b_total_time_zero():
    rb = _row(1, commit_sha="sha-z", total_time=0.0, scan_completed_at=_dt(10))
    sms = _row(2, commit_sha="sha-z", total_time=5.0, scan_completed_at=_dt(10, 1))

    result = build_comparisons([rb], [sms])

    assert len(result) == 1
    assert result[0]["speedup"] is None


def test_speedup_none_for_one_sided_rows():
    rb = _row(1, commit_sha="solo", total_time=5.0, scan_completed_at=_dt(10))

    result = build_comparisons([rb], [])

    assert result[0]["speedup"] is None


def test_ordering_newest_first_none_dated_last():
    rb_none_date = _row(1, commit_sha="c1", scan_completed_at=None)
    rb_old = _row(2, commit_sha="c2", scan_completed_at=_dt(5))
    rb_new = _row(3, commit_sha="c3", scan_completed_at=_dt(20))

    result = build_comparisons([rb_none_date, rb_old, rb_new], [])

    scan_ids = [row["route_b"]["scan_id"] for row in result]
    assert scan_ids == [3, 2, 1]


def test_ordering_stable_among_none_dated_rows():
    rb_a = _row(1, commit_sha="none-a", scan_completed_at=None)
    rb_b = _row(2, commit_sha="none-b", scan_completed_at=None)
    rb_c = _row(3, commit_sha="none-c", scan_completed_at=None)

    result = build_comparisons([rb_a, rb_b, rb_c], [])

    scan_ids = [row["route_b"]["scan_id"] for row in result]
    assert scan_ids == [1, 2, 3]


def test_commit_match_prefers_closest_completed_at_when_multiple_candidates():
    rb = _row(1, commit_sha="shared", scan_completed_at=_dt(10))
    sms_far = _row(2, commit_sha="shared", scan_completed_at=_dt(2))
    sms_near = _row(3, commit_sha="shared", scan_completed_at=_dt(10, 3))

    result = build_comparisons([rb], [sms_far, sms_near])

    matched = [row for row in result if row["match_kind"] == "commit"]
    assert len(matched) == 1
    assert matched[0]["sms"]["scan_id"] == 3

    leftover = [row for row in result if row["match_kind"] == "one-sided"]
    assert len(leftover) == 1
    assert leftover[0]["sms"]["scan_id"] == 2


def test_empty_inputs_produce_empty_output():
    assert build_comparisons([], []) == []


def test_commit_match_ignores_empty_commit_sha():
    rb = _row(1, commit_sha="", scan_ref="", scan_completed_at=_dt(1))
    sms = _row(2, commit_sha="", scan_ref="", scan_completed_at=_dt(1, 1))

    result = build_comparisons([rb], [sms])

    # empty commit_sha/scan_ref never key a match; both fall through to one-sided
    assert all(row["match_kind"] == "one-sided" for row in result)
    assert len(result) == 2
