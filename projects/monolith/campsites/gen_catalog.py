"""One-shot generator for the static BC Parks campsites catalog.

Pulls the GoingToCamp catalog (IDs, names, region, timezone, booking slug) and
resolves coordinates once via Open-Meteo geocoding (validated to a BC bounding
box), writing campsites/catalog.json. Re-run manually when BC Parks adds a park.
"""

import json
import time
import urllib.parse
import urllib.request

BASE = "https://camping.bcparks.ca"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; jomcgi-campsites/1.0; +https://jomcgi.dev/app/campsites)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": f"{BASE}/",
    "Origin": BASE,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}
# British Columbia bounding box (generous): lat 48..60, lon -139..-114.
BC = {"lat_lo": 48.0, "lat_hi": 60.5, "lon_lo": -139.5, "lon_hi": -113.5}

# Coordinates the Open-Meteo geocoder cannot resolve (unusual names, trails,
# Indigenous place names). Keyed by resourceLocationId, hand-verified to the
# park area. Weather is regional so park-centroid precision is sufficient.
MANUAL_COORDS = {
    -2147483642: (53.030, -119.230),  # Berg Lake Trail (Mount Robson)
    -2147483618: (49.064, -120.782),  # E. C. Manning
    -2147483593: (49.833, -120.500),  # Kentucky Alleyne
    -2147483592: (49.133, -118.983),  # Kettle River
    -2147483550: (49.556, -123.233),  # Porteau Cove
    -2147483535: (49.270, -121.480),  # Silver Lake (near Hope)
    -2147483627: (49.083, -121.433),  # Sx̱otsaqel / Chilliwack Lake
    -2147483637: (59.350, -129.100),  # Ta Ch'ila / Boya Lake
}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _curl_json(path):
    """Fetch a camping.bcparks.ca API path via the curl binary.

    The Azure WAF fronting the reservation API fingerprints the TLS handshake
    and rejects Python's urllib/httpx stack with a 403, but accepts curl. We
    only need this for the one-shot catalog pull, so shelling out to curl keeps
    the generator dependency-free. (The runtime job uses curl_cffi for the same
    reason.)
    """
    import subprocess

    hdr = []
    for k, v in HEADERS.items():
        hdr += ["-H", f"{k}: {v}"]
    out = subprocess.run(
        ["curl", "-sS", "-m", "30", *hdr, f"{BASE}{path}"],
        capture_output=True,
        check=True,
        timeout=35,
    ).stdout
    return json.loads(out)


def fetch_catalog():
    maps = _curl_json("/api/maps")
    rl = _curl_json("/api/resourceLocation")
    # resourceLocationId -> (park_map_id, title) from map links
    park_map = {}
    for m in maps:
        for ln in m.get("mapLinks", []) or []:
            rid = ln.get("resourceLocationId")
            if rid is None:
                continue
            title = None
            for loc in ln.get("localizations", []) or []:
                if loc.get("cultureName") == "en-CA":
                    title = loc.get("title")
            park_map.setdefault(rid, (ln.get("childMapId"), title))
    rows = []
    for e in rl:
        rid = e.get("resourceLocationId")
        if rid not in park_map:
            continue
        park_map_id, title = park_map[rid]
        en = None
        for loc in e.get("localizedValues", []) or []:
            if loc.get("cultureName") == "en-CA":
                en = loc
                break
        en = en or {}
        name = en.get("shortName") or en.get("fullName") or title or str(rid)
        website = (en.get("website") or "").strip()
        gps = (e.get("gpsCoordinates") or "").strip()
        rows.append(
            {
                "resource_location_id": rid,
                "park_map_id": park_map_id,
                "name": name,
                "region": e.get("region") or "",
                "iana_tz": e.get("ianaTimeZone") or "America/Vancouver",
                "website": website,
                "gps_raw": gps,
            }
        )
    return rows


def _in_bc(lat, lon):
    return (
        lat is not None
        and lon is not None
        and BC["lat_lo"] <= lat <= BC["lat_hi"]
        and BC["lon_lo"] <= lon <= BC["lon_hi"]
    )


def _candidates(name):
    """Yield geocoder-friendly name variants, most specific first."""
    seen = []

    def add(c):
        c = c.strip()
        if c and c not in seen:
            seen.append(c)

    add(name)
    # English part inside parentheses: "sx...w (Okanagan Falls)" -> "Okanagan Falls"
    if "(" in name and ")" in name:
        add(name[name.index("(") + 1 : name.rindex(")")])
    # take the head before separators: " - ", " / ", " & ", " [", ","
    head = name
    for sep in [" - ", " / ", " & ", " [", ","]:
        if sep in head:
            head = head.split(sep)[0]
    add(head)
    # drop descriptor suffixes
    stripped = head
    for suf in [" Backcountry", " Marine", " Trail", " South", " North"]:
        if stripped.endswith(suf):
            stripped = stripped[: -len(suf)]
    add(stripped)
    add(stripped + " Park")
    return seen


def geocode(name):
    for cand in _candidates(name):
        q = urllib.parse.urlencode({"name": cand, "count": 8, "language": "en"})
        url = f"https://geocoding-api.open-meteo.com/v1/search?{q}"
        # Offline generator: skip a flaky geocoder candidate and try the next.
        try:  # nosemgrep: no-broad-except-swallow
            data = _get(url)
        except Exception:
            time.sleep(0.4)
            continue
        # prefer a Canadian BC-boxed hit whose admin1 mentions columbia
        ranked = []
        for res in data.get("results", []) or []:
            lat, lon = res.get("latitude"), res.get("longitude")
            if not _in_bc(lat, lon) or res.get("country_code") not in (None, "CA"):
                continue
            admin1 = (res.get("admin1") or "").lower()
            score = 2 if "columbia" in admin1 else 1
            # a result named like a park ranks above a bare town of the same name
            if "park" in (res.get("name") or "").lower():
                score += 1
            ranked.append((score, lat, lon, res.get("name")))
        if ranked:
            ranked.sort(reverse=True)
            _, lat, lon, rn = ranked[0]
            return lat, lon, rn
        time.sleep(0.1)
    return None


def main():
    rows = fetch_catalog()
    out = []
    needs_manual = []
    for row in rows:
        lat = lon = None
        source = None
        if row["gps_raw"]:
            # Offline generator: a malformed gps string falls through to the geocoder.
            try:  # nosemgrep: no-broad-except-swallow
                a, b = row["gps_raw"].split(",")
                lat, lon = float(a), float(b)
                source = "gps"
            except Exception:
                lat = lon = None
        if lat is None:
            g = geocode(row["name"])
            if g:
                lat, lon, _ = g
                source = "geocode"
        if lat is None and row["resource_location_id"] in MANUAL_COORDS:
            lat, lon = MANUAL_COORDS[row["resource_location_id"]]
            source = "manual"
        entry = {
            "resource_location_id": row["resource_location_id"],
            "park_map_id": row["park_map_id"],
            "name": row["name"],
            "region": row["region"],
            "latitude": round(lat, 5) if lat is not None else None,
            "longitude": round(lon, 5) if lon is not None else None,
            "iana_tz": row["iana_tz"],
            "website": row["website"],
            "coord_source": source,
        }
        out.append(entry)
        if lat is None:
            needs_manual.append(row["name"])
    out.sort(key=lambda r: r["name"])
    print(
        json.dumps(
            {
                "count": len(out),
                "resolved": sum(1 for r in out if r["latitude"] is not None),
                "needs_manual": needs_manual,
            },
            indent=2,
        )
    )
    with open("/tmp/catalog.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
