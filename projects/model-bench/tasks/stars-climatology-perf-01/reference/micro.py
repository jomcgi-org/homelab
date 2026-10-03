"""Task-local micro-optimisation: preallocate the output arrays, retain rescans."""


def summarize(sites, rows):
    first_seen = {}
    for index, row in enumerate(rows):
        if 1 <= row["month"] <= 12:
            first_seen.setdefault(row["site_id"], index)
    output = []
    for site in sites:
        site_id = site["id"]
        clear, dark = [0] * 12, [0] * 12
        for month in range(1, 13):
            clear_hours = dark_hours = 0
            for row in rows:
                if row["site_id"] == site_id and row["month"] == month:
                    clear_hours += row["clear_dark_hours"]
                    dark_hours += row["dark_hours"]
            clear[month - 1] = clear_hours
            dark[month - 1] = dark_hours
        if sum(dark) <= 0:
            continue
        output.append(
            {
                "id": site_id,
                "name": site["name"],
                "lat": site["lat"],
                "lon": site["lon"],
                "clear": clear,
                "dark": dark,
            }
        )
    output.sort(key=lambda site: (-sum(site["clear"]), first_seen[site["id"]]))
    return {"sites": output, "count": len(output)}
