"""Task-local indexed reference, excluded from the model-visible fixture."""


def summarize(sites, rows):
    metadata = {site["id"]: site for site in sites}
    by_site = {}
    for row in rows:
        month, site_id = row["month"], row["site_id"]
        if not 1 <= month <= 12 or site_id not in metadata:
            continue
        if site_id not in by_site:
            site = metadata[site_id]
            by_site[site_id] = {
                "id": site_id,
                "name": site["name"],
                "lat": site["lat"],
                "lon": site["lon"],
                "clear": [0] * 12,
                "dark": [0] * 12,
            }
        result = by_site[site_id]
        result["clear"][month - 1] += row["clear_dark_hours"]
        result["dark"][month - 1] += row["dark_hours"]
    output = [site for site in by_site.values() if sum(site["dark"]) > 0]
    output.sort(key=lambda site: sum(site["clear"]), reverse=True)
    return {"sites": output, "count": len(output)}
