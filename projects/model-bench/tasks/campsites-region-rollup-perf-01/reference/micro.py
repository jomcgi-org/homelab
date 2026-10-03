"""Model-hidden constant-factor variant retaining every campground-day row scan."""

from datetime import date, timedelta


def summarize(campgrounds, availability, weather, today):
    if not campgrounds:
        return {"status": 503, "detail": "campsites data unavailable"}
    start = date.fromisoformat(today) - timedelta(days=1)
    days = [(start + timedelta(days=i)).isoformat() for i in range(15)]
    output = []
    for region in sorted({park["region"] for park in campgrounds}):
        parks = good_days = open_parks = best_score = 0
        for park in campgrounds:
            if park["region"] != region:
                continue
            parks += 1
            park_id = park["resource_location_id"]
            park_good = 0
            for day in days:
                available = False
                score, good = 0, False
                for row in availability:
                    if row["resource_location_id"] == park_id and row["date"] == day:
                        available = row["has_availability"]
                for row in weather:
                    if row["resource_location_id"] == park_id and row["date"] == day:
                        score, good = row["sunny_score"], row["is_good"]
                if available:
                    if score > best_score:  # noqa: PLR1730 - intentional constant-factor variant
                        best_score = score
                    park_good += good
            good_days += park_good
            open_parks += park_good > 0
        output.append(
            {
                "region": region,
                "parks": parks,
                "best_score": best_score,
                "good_days": good_days,
                "open_parks": open_parks,
            }
        )
    output.sort(key=lambda result: (-result["best_score"], result["region"]))
    return {"count": len(output), "regions": output}
