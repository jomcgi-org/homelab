from datetime import datetime, timedelta, timezone

from delivery_stats import delivery_statistics


def test_statistics_include_idle_weeks_and_weight_each_merge_once():
    created = datetime(2026, 11, 2, tzinfo=timezone.utc)
    rows = [
        dict(
            issue_key=f"HOME-{i}",
            pr_number=i,
            created_at=created,
            merged_at=created + timedelta(hours=hours) if hours else None,
            state=state,
            review_rounds=rounds,
        )
        for i, (hours, rounds, state) in enumerate(
            [
                (2, 0, "Shipped"),
                (4, 0, "Shipped"),
                (336, 2, "Shipped"),
                (None, 0, "In review"),
                (None, 0, "Stopped"),
            ]
        )
    ]
    stats, charts = delivery_statistics(reversed(rows))
    assert [p["value"] for p in charts["shippedPerWeek"]] == [2, 0, 1]
    assert [p["value"] for p in charts["firstPassReviewRate"]] == [100, 0]
    assert stats["mergeHours"]["value"] == 114
    assert stats["reviewRounds"]["value"] == 5 / 3
    assert stats["firstPass"]["value"] == 200 / 3
    assert stats["active"]["value"] == 1
    assert stats["shipped"]["value"] == 3


def test_empty_public_issue_set():
    stats, charts = delivery_statistics([])
    assert set(stats) == {"active", "shipped"}
    assert all(not points for points in charts.values())
