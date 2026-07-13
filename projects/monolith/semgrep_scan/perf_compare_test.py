"""Tests for perf_compare.build_comparisons.

All inputs are hand-built literals with fixed timezone-aware datetimes; no
clock/random calls, so tests are fully deterministic.
"""

from __future__ import annotations

import datetime

import pytest

from semgrep_scan.perf_compare import (
    build_aggregates,
    build_cohort_aggregates,
    build_comparisons,
    build_distributions,
)

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


# ── build_aggregates ─────────────────────────────


def _pair(is_full: bool, rb_time: float, sms_time: float) -> dict:
    """A matched (two-sided) comparison row, the only kind aggregates count."""
    return {
        "commit_sha": "c",
        "scan_ref": "r",
        "is_full_scan": is_full,
        "branch": "main",
        "route_b": {
            "scan_id": 1,
            "total_time": rb_time,
            "findings_total": 0,
            "scan_completed_at": None,
        },
        "sms": {
            "scan_id": 2,
            "total_time": sms_time,
            "findings_total": 0,
            "scan_completed_at": None,
        },
        "speedup": None,
        "match_kind": "commit",
    }


def test_aggregates_medians_and_speedup_per_bucket():
    comparisons = [
        _pair(False, 10.0, 30.0),
        _pair(False, 20.0, 40.0),
        _pair(True, 100.0, 400.0),
    ]
    agg = build_aggregates(comparisons)
    # PR bucket: medians 15 and 35, speedup 35/15
    assert agg["pr"]["pairs"] == 2
    assert agg["pr"]["homelab_median"] == 15.0
    assert agg["pr"]["managed_median"] == 35.0
    assert agg["pr"]["speedup"] == pytest.approx(35.0 / 15.0)
    # full bucket: single pair
    assert agg["full"]["pairs"] == 1
    assert agg["full"]["speedup"] == pytest.approx(4.0)


def test_aggregates_ignore_one_sided_rows():
    one_sided = {
        "commit_sha": "c",
        "scan_ref": "r",
        "is_full_scan": False,
        "branch": "main",
        "route_b": {
            "scan_id": 1,
            "total_time": 5.0,
            "findings_total": 0,
            "scan_completed_at": None,
        },
        "sms": None,
        "speedup": None,
        "match_kind": "one-sided",
    }
    agg = build_aggregates([one_sided])
    assert agg["pr"]["pairs"] == 0
    assert agg["pr"]["speedup"] is None


def test_aggregates_empty_input_has_stable_shape():
    agg = build_aggregates([])
    for key in ("pr", "full"):
        assert agg[key] == {
            "pairs": 0,
            "homelab_median": None,
            "managed_median": None,
            "speedup": None,
            "findings_pairs": 0,
            "findings_agree": 0,
        }


def test_aggregates_speedup_guards_zero_homelab_time():
    agg = build_aggregates([_pair(False, 0.0, 30.0)])
    assert agg["pr"]["pairs"] == 1
    assert agg["pr"]["speedup"] is None


def test_aggregates_findings_parity():
    agree = _pair(False, 10.0, 30.0)
    agree["route_b"]["findings_total"] = 7
    agree["sms"]["findings_total"] = 7
    disagree = _pair(False, 10.0, 30.0)
    disagree["route_b"]["findings_total"] = 7
    disagree["sms"]["findings_total"] = 9
    unknown = _pair(False, 10.0, 30.0)
    unknown["route_b"]["findings_total"] = None

    agg = build_aggregates([agree, disagree, unknown])

    assert agg["pr"]["pairs"] == 3
    # the pair with a None findings side does not count toward parity
    assert agg["pr"]["findings_pairs"] == 2
    assert agg["pr"]["findings_agree"] == 1


# ── build_distributions ──────────────────────────


def test_distributions_bucket_by_type_and_side():
    homelab = [
        _row(1, total_time=1.0),
        _row(2, total_time=2.0),
        _row(3, total_time=3.0),
        _row(4, is_full_scan=True, total_time=300.0),
    ]
    managed = [_row(5, total_time=40.0), _row(6, total_time=60.0)]

    dist = build_distributions(homelab, managed)

    assert dist["pr"]["homelab"]["n"] == 3
    assert dist["pr"]["homelab"]["p50"] == 2.0
    assert dist["pr"]["homelab"]["min"] == 1.0
    assert dist["pr"]["homelab"]["max"] == 3.0
    # p90 over [1,2,3]: linear interpolation at pos 1.8 -> 2.8
    assert dist["pr"]["homelab"]["p90"] == pytest.approx(2.8)
    assert dist["pr"]["managed"]["n"] == 2
    assert dist["pr"]["managed"]["p50"] == 50.0
    assert dist["full"]["homelab"]["n"] == 1
    assert dist["full"]["homelab"]["p50"] == 300.0
    assert dist["full"]["managed"] == {
        "n": 0,
        "p50": None,
        "p90": None,
        "min": None,
        "max": None,
    }


def test_distributions_skip_rows_without_total_time():
    homelab = [_row(1, total_time=None), _row(2, total_time=5.0)]

    dist = build_distributions(homelab, [])

    assert dist["pr"]["homelab"]["n"] == 1
    assert dist["pr"]["homelab"]["p50"] == 5.0


def test_distributions_empty_input_has_stable_shape():
    dist = build_distributions([], [])
    for bucket in ("pr", "full"):
        for side in ("homelab", "managed"):
            assert dist[bucket][side] == {
                "n": 0,
                "p50": None,
                "p90": None,
                "min": None,
                "max": None,
            }


# ── build_cohort_aggregates ──────────────────────


def _cohort_pair(file_count, changed_lines, languages, homelab_t, managed_t):
    return {
        "is_full_scan": False,
        "route_b": {"total_time": homelab_t},
        "sms": {"total_time": managed_t},
        "speedup": managed_t / homelab_t,
        "cohort": {
            "file_count": file_count,
            "changed_lines": changed_lines,
            "languages": languages,
        },
    }


def test_build_cohort_aggregates_segments_by_files_lines_language():
    comps = [
        _cohort_pair(1, 10, {"python": 10}, 3.0, 45.0),
        _cohort_pair(2, 30, {"python": 30}, 4.0, 44.0),
        _cohort_pair(8, 300, {"go": 300}, 20.0, 60.0),
    ]
    out = build_cohort_aggregates(comps)
    assert out["total_pairs"] == 3

    by_files = {g["label"]: g["pairs"] for g in out["by_files"]}
    assert by_files == {"1 file": 1, "2-4": 1, "5-9": 1}

    by_lines = {g["label"]: g["pairs"] for g in out["by_lines"]}
    assert by_lines == {"<50": 2, "200-499": 1}

    by_lang = {g["label"]: g["pairs"] for g in out["by_language"]}
    assert by_lang == {"python": 2, "go": 1}

    one_file = next(g for g in out["by_files"] if g["label"] == "1 file")
    assert one_file["speedup"] == 45.0 / 3.0


def test_build_cohort_aggregates_ignores_pairs_without_cohort_or_full_scans():
    comps = [
        {
            "is_full_scan": False,
            "route_b": {"total_time": 3.0},
            "sms": {"total_time": 40.0},
            "speedup": 13.3,
            "cohort": None,
        },
        {
            "is_full_scan": True,
            "route_b": {"total_time": 200.0},
            "sms": {"total_time": 400.0},
            "speedup": 2.0,
            "cohort": {
                "file_count": 5,
                "changed_lines": 100,
                "languages": {"python": 100},
            },
        },
        {
            "is_full_scan": False,
            "route_b": None,
            "sms": {"total_time": 40.0},
            "speedup": None,
            "cohort": None,
        },
    ]
    out = build_cohort_aggregates(comps)
    assert out["total_pairs"] == 0
    assert out["by_files"] == []
    assert out["by_language"] == []
