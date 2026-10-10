"""Model-hidden algorithmic calibration reference for campsites-rollup-seeded-v1."""

from datetime import date, timedelta


def summarize(campgrounds, availability, weather, today):
    if not campgrounds:
        return {"status": 503, "detail": "campsites data unavailable"}
    low = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    high = (date.fromisoformat(today) + timedelta(days=13)).isoformat()
    metadata = {p["resource_location_id"]: p["region"] for p in campgrounds}
    regions = {}
    for park in campgrounds:
        region = park["region"]
        if region not in regions:
            regions[region] = {
                "region": region,
                "parks": 0,
                "best_score": 0,
                "good_days": 0,
                "open_parks": 0,
            }
        regions[region]["parks"] += 1
    avail = {}
    forecasts = {}
    for row in availability:
        rid, day = row["resource_location_id"], row["date"]
        if rid in metadata and low <= day <= high:
            avail[(rid, day)] = row["has_availability"]
    for row in weather:
        rid, day = row["resource_location_id"], row["date"]
        if rid in metadata and low <= day <= high:
            forecasts[(rid, day)] = (row["sunny_score"], row["is_good"])
    opened = set()
    for key, available in avail.items():
        if not available:
            continue
        rid, day = key
        score, good = forecasts.get(key, (0, False))
        record = regions[metadata[rid]]
        record["best_score"] = max(record["best_score"], score)
        if good:
            record["good_days"] += 1
            opened.add(rid)
    for rid in opened:
        regions[metadata[rid]]["open_parks"] += 1
    output = sorted(regions.values(), key=lambda r: (-r["best_score"], r["region"]))
    return {"count": len(output), "regions": output}
