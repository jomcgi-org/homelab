import pytest

from bb_usage import (
    aggregate,
    concentration,
    compare,
    format_bytes,
    normalise_pattern,
    render_report,
    roll_up_daily,
    source_key,
    top_invocations,
)


def test_normalise_pattern():
    assert normalise_pattern(["//foo", "//bar"]) == "//foo"
    assert normalise_pattern("//foo") == "//foo"
    assert normalise_pattern(None) == ""
    assert len(normalise_pattern("x" * 100)) == 60


def test_source_key_local():
    assert source_key({"command": "test"}) == ("LOCAL", "test", "")
    assert source_key({"role": "", "pattern": []})[0] == "LOCAL"


def test_aggregate():
    invocations = [
        {
            "role": "CI",
            "command": "test",
            "pattern": ["//..."],
            "cacheStats": {
                "totalDownloadSizeBytes": "1000",
                "totalUploadSizeBytes": "200",
                "totalDownloadTransferredSizeBytes": "10",
                "totalUploadTransferredSizeBytes": "20",
            },
        },
        {
            "role": "CI",
            "command": "test",
            "pattern": "//...",
            "cacheStats": {"totalDownloadSizeBytes": "300"},
        },
        {"command": "build", "pattern": None},
    ]
    result = aggregate(invocations, 2)
    assert result["totals"] == {
        "download_bytes": 1300,
        "upload_bytes": 200,
        "download_transferred_bytes": 10,
        "upload_transferred_bytes": 20,
        "invocations": 3,
    }
    assert result["per_day"]["download_bytes"] == 650
    assert result["by_role"]["CI"]["download_bytes"] == 1300
    assert result["by_role"]["LOCAL"]["invocations"] == 1
    assert result["by_source"][0]["download_bytes"] == 1300
    assert sum(row["download_share"] for row in result["by_source"]) == pytest.approx(
        1.0
    )


def test_aggregate_empty_and_zero_window():
    result = aggregate([], 0)
    assert result["totals"]["invocations"] == 0
    assert result["per_day"]["download_bytes"] == 0
    assert result["by_source"] == []


def test_concentration_empty():
    result = concentration([])
    assert result == {
        "invocations": 0,
        "total_bytes": 0,
        "p50_bytes": 0.0,
        "p90_bytes": 0.0,
        "p99_bytes": 0.0,
        "max_bytes": 0,
        "top_shares": {
            "1%": {"invocations": 0, "bytes": 0, "share": 0.0},
            "5%": {"invocations": 0, "bytes": 0, "share": 0.0},
            "10%": {"invocations": 0, "bytes": 0, "share": 0.0},
        },
    }


def test_concentration_known_values():
    result = concentration([100] + [1] * 99)
    assert result["invocations"] == 100
    assert result["total_bytes"] == 199
    assert result["p50_bytes"] == 1.0
    assert result["max_bytes"] == 100
    assert result["top_shares"]["1%"] == {
        "invocations": 1,
        "bytes": 100,
        "share": pytest.approx(100 / 199),
    }


def test_concentration_zero_share():
    assert all(
        row["share"] == 0.0 for row in concentration([0, 0])["top_shares"].values()
    )


def test_top_invocations():
    invocations = [
        {
            "id": {"invocationId": "small"},
            "command": "test",
            "cacheStats": {"totalDownloadSizeBytes": "10", "totalUploadSizeBytes": "2"},
        },
        {
            "id": {"invocationId": "large"},
            "role": "CI",
            "command": "build",
            "pattern": ["//..."],
            "branchName": "main",
            "success": True,
            "cacheStats": {
                "totalDownloadSizeBytes": "100",
                "totalUploadSizeBytes": "20",
            },
        },
        {"id": {"invocationId": "missing-stats"}},
    ]
    rows = top_invocations(invocations, 2)
    assert [row["invocation_id"] for row in rows] == ["large", "small"]
    assert rows[0]["url"] == "https://app.buildbuddy.io/invocation/large"
    assert rows[0]["pattern"] == "//..."
    assert rows[1]["role"] == "LOCAL"
    assert top_invocations(invocations, 1)[0]["download_bytes"] == 100
    assert top_invocations(invocations, 3)[-1]["download_bytes"] == 0


def _snapshot(download, upload=100, source_download=None, days=1):
    source_download = download if source_download is None else source_download
    return {
        "window": {"days": days},
        "per_day": {"download_bytes": download, "upload_bytes": upload},
        "by_source": [
            {
                "role": "CI",
                "command": "test",
                "pattern": "//...",
                "download_bytes": source_download,
            }
        ],
    }


def test_compare_progress_and_movers():
    baseline = _snapshot(100, source_download=100)
    half = compare(baseline, _snapshot(50), 0.5)
    assert half["progress"] == 1.0 and half["met"] is True
    quarter = compare(baseline, _snapshot(75), 0.5)
    assert quarter["progress"] == 0.5 and quarter["met"] is False
    increase = compare(baseline, _snapshot(125), 0.5)
    assert increase["progress"] == 0.0 and increase["download_change"] > 0
    # Both sides carry the same source key, so the mover is the signed per-day delta.
    assert increase["movers"][0]["delta_per_day"] == 25


def test_compare_movers_rank_dropped_source_first():
    """A source that stops downloading is the largest reduction, so it must lead."""
    baseline = {
        "window": {"days": 1},
        "per_day": {"download_bytes": 130, "upload_bytes": 0},
        "by_source": [
            {
                "role": "CI",
                "command": "run",
                "pattern": "//push",
                "download_bytes": 100,
            },
            {"role": "CI", "command": "test", "pattern": "//...", "download_bytes": 30},
        ],
    }
    current = {
        "window": {"days": 1},
        "per_day": {"download_bytes": 40, "upload_bytes": 0},
        "by_source": [
            {"role": "CI", "command": "test", "pattern": "//...", "download_bytes": 40}
        ],
    }
    movers = compare(baseline, current, 0.5)["movers"]
    assert movers[0] == {
        "role": "CI",
        "command": "run",
        "pattern": "//push",
        "baseline_per_day": 100.0,
        "current_per_day": 0.0,
        "delta_per_day": -100.0,
    }
    assert movers[-1]["delta_per_day"] == 10.0


def test_compare_zero_baseline():
    assert compare(_snapshot(0, upload=0), _snapshot(10, upload=10))["progress"] == 0.0


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1_000_000) == "1.0 MB"
    assert format_bytes(1_000_000_000) == "1.0 GB"
    assert format_bytes(1_000_000_000_000) == "1.0 TB"


def test_roll_up_daily_empty():
    assert roll_up_daily([]) == []


def test_roll_up_daily_sums_sub_daily_buckets():
    buckets = [
        {
            "bucket_start": "2026-07-21T00:00:00+00:00",
            "download_bytes": 1,
            "upload_bytes": 2,
            "builds": 3,
            "action_cache_hits": 4,
            "action_cache_misses": 5,
        },
        {
            "bucket_start": "2026-07-21T06:00:00+00:00",
            "download_bytes": 10,
            "upload_bytes": 20,
            "builds": 30,
            "action_cache_hits": 40,
            "action_cache_misses": 50,
        },
        {
            "bucket_start": "2026-07-21T12:00:00+00:00",
            "download_bytes": 100,
            "upload_bytes": 200,
            "builds": 300,
            "action_cache_hits": 400,
            "action_cache_misses": 500,
        },
        {
            "bucket_start": "2026-07-21T18:00:00+00:00",
            "download_bytes": 1000,
            "upload_bytes": 2000,
            "builds": 3000,
            "action_cache_hits": 4000,
            "action_cache_misses": 5000,
        },
    ]
    assert roll_up_daily(buckets) == [
        {
            "date": "2026-07-21",
            "download_bytes": 1111,
            "upload_bytes": 2222,
            "builds": 3333,
            "action_cache_hits": 4444,
            "action_cache_misses": 5555,
        }
    ]


def test_roll_up_daily_sorts_dates():
    buckets = [
        {"bucket_start": "2026-07-22T00:00:00+00:00", "builds": 2},
        {"bucket_start": "2026-07-20T00:00:00+00:00", "builds": 1},
    ]
    rows = roll_up_daily(buckets)
    assert [row["date"] for row in rows] == ["2026-07-20", "2026-07-22"]
    assert [row["builds"] for row in rows] == [1, 2]


def test_roll_up_daily_skips_invalid_bucket_starts():
    buckets = [{"builds": 1}, {"bucket_start": "not-a-timestamp", "builds": 2}]
    assert roll_up_daily(buckets) == []


def test_render_truncation_warning():
    snapshot = {
        "window": {"days": 7},
        "totals": {},
        "by_source": [],
        "by_role": {},
        "truncated": True,
    }
    assert "floor, not a total" in render_report(snapshot, None)
    snapshot["truncated"] = False
    assert "floor, not a total" not in render_report(snapshot, None)


def test_render_old_snapshot_compatibility():
    snapshot = {"window": {"days": 7}, "totals": {}, "by_source": [], "by_role": {}}
    assert render_report(snapshot, None)


def test_render_concentration_and_outlier():
    snapshot = {
        "window": {"days": 7},
        "totals": {},
        "by_source": [],
        "by_role": {},
        "concentration": {
            "p50_bytes": 1,
            "p90_bytes": 2,
            "p99_bytes": 3,
            "max_bytes": 4,
            "top_shares": {"1%": {"bytes": 4, "share": 1.0}},
        },
        "top_invocations": [
            {
                "download_bytes": 4,
                "role": "CI",
                "command": "test",
                "pattern": "//...",
                "branch": "main",
                "url": "https://app.buildbuddy.io/invocation/abc",
            }
        ],
    }
    report = render_report(snapshot, None)
    assert "Concentration" in report
    assert "https://app.buildbuddy.io/invocation/abc" in report
