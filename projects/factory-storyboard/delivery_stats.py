"""Storyboard metrics from the public issue set and its connector deliveries."""

from collections import defaultdict
from datetime import timedelta, timezone
from statistics import mean


def delivery_statistics(issues):
    """Rows contain issue_key, created_at, merged_at, state, pr_number and review_rounds.

    The connector increments review_rounds on requests for another coding
    round. A merged PR with zero such requests passed on its first review.
    """
    rows = sorted(issues, key=lambda row: row["issue_key"])
    weeks = defaultdict(list)
    durations = []
    reviews = []
    active = 0
    for row in rows:
        merged = row["merged_at"]
        if merged is None:
            active += row["state"] in ("Working", "In review", "In merge queue")
            continue
        day = merged.astimezone(timezone.utc).date()
        weeks[day - timedelta(days=day.weekday())].append(row["review_rounds"] == 0)
        hours = (merged - row["created_at"]).total_seconds() / 3600
        durations.append({"label": row["issue_key"], "value": hours})
        reviews.append(
            {"label": f"PR #{row['pr_number']}", "value": row["review_rounds"] + 1}
        )

    stats = {}

    def stat(key, label, value, unit):
        stats[key] = {"label": label, "value": value, "unit": unit}

    stat("shipped", "Shipped", len(durations), "issues")
    stat("active", "In progress", active, "issues")
    if durations:
        stat(
            "mergeHours", "Issue to merge", mean(p["value"] for p in durations), "hours"
        )
        stat(
            "reviewRounds", "Review rounds", mean(p["value"] for p in reviews), "rounds"
        )
        stat(
            "firstPass",
            "First-pass review",
            100 * sum(map(sum, weeks.values())) / len(reviews),
            "%",
        )

    shipped = []
    rates = []
    if weeks:
        week = min(weeks)
        last = max(weeks)
        while week <= last:
            passes = weeks.get(week, [])
            label = f"Week of {week.isoformat()}"
            shipped.append({"label": label, "value": len(passes)})
            if passes:
                rates.append({"label": label, "value": 100 * mean(passes)})
            week += timedelta(days=7)
    return stats, {
        "shippedPerWeek": shipped,
        "issueToMergeHours": durations,
        "reviewRounds": reviews,
        "firstPassReviewRate": rates,
    }
