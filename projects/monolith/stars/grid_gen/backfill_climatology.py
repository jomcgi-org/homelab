"""Offline ERA5 climatology backfill for the stars historical heatmap (ADR 009).

Fetches ~5yr of hourly ERA5 (Open-Meteo archive) per grid point, scores each
dark hour with the same Q = D x C x W model as the live pipeline, and aggregates
sufficient statistics by month-of-year into climatology.json.

Pure stdlib: NOAA solar elevation (so D matches astral within tolerance) and the
Q-factor formulas replicated from projects/monolith/stars/scoring.py (KEEP IN
SYNC). Output is uploaded to SeaweedFS (s3://stars/climatology.json) and ingested
by the stars.load_climatology job into stars.site_month_climatology.
"""

import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
START, END = "2021-01-01", "2025-12-31"
CIVIL, ASTRO, CLOUD_SPAN = -6.0, -18.0, 45.0


def sun_elevation_deg(lat, lon, dt):
    """NOAA solar elevation (degrees) for a naive-UTC datetime."""
    doy = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60.0
    g = 2 * math.pi / 365.0 * (doy - 1 + (hour - 12) / 24.0)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    tst = hour * 60.0 + eqtime + 4.0 * lon  # true solar time (minutes), UTC
    ha = math.radians(tst / 4.0 - 180.0)  # hour angle
    latr = math.radians(lat)
    cos_zen = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(
        decl
    ) * math.cos(ha)
    cos_zen = max(-1.0, min(1.0, cos_zen))
    return 90.0 - math.degrees(math.acos(cos_zen))


def darkness_factor(e):
    if e >= CIVIL:
        return 0.0
    if e <= ASTRO:
        return 1.0
    return (CIVIL - e) / (CIVIL - ASTRO)


def cloud_factor(cloud, d):
    allowance = 5.0 + 5.0 * d
    return max(0.0, 1.0 - max(0.0, cloud - allowance) / CLOUD_SPAN)


def _humidity_score(h):
    if h < 70:
        return 100.0
    if h < 85:
        return 100.0 - (h - 70) * 3.33
    return max(0.0, 50.0 - (h - 85) * 3.33)


def _wind_score(w):
    if w < 5:
        return 100.0
    if w < 10:
        return 100.0 - (w - 5) * 10.0
    return max(0.0, 50.0 - (w - 10) * 5.0)


def _dew_score(spread):
    if spread > 5:
        return 100.0
    if spread > 2:
        return 100.0 - (5 - spread) * 16.67
    return max(0.0, 50.0 - (2 - spread) * 25.0)


def weather_modifier(humidity, wind, dew_spread):
    # fog has no ERA5 equivalent -> fog_score is 100 (matches the default path).
    avg = (
        _humidity_score(humidity) + 100.0 + _wind_score(wind) + _dew_score(dew_spread)
    ) / 4.0
    return 0.7 + 0.3 * (avg / 100.0)


def score_point(lat, lon, hourly):
    """Aggregate sufficient stats by month-of-year for one point's ERA5 series."""
    times = hourly["time"]
    cc = hourly["cloud_cover"]
    temp = hourly["temperature_2m"]
    rh = hourly["relative_humidity_2m"]
    wind = hourly["wind_speed_10m"]
    dew = hourly["dew_point_2m"]
    by_month = {}  # month -> [count, sum_q, sum_d, sum_c]
    for i, t in enumerate(times):
        if cc[i] is None or temp[i] is None or dew[i] is None:
            continue
        dt = datetime.fromisoformat(t)  # naive UTC
        d = darkness_factor(sun_elevation_deg(lat, lon, dt))
        if d <= 0:
            continue
        c = cloud_factor(cc[i], d)
        w = weather_modifier(rh[i] or 100, wind[i] or 0, temp[i] - dew[i])
        q = d * c * w * 100.0
        if q <= 0:
            continue
        b = by_month.setdefault(dt.month, [0, 0.0, 0.0, 0.0])
        b[0] += 1
        b[1] += q
        b[2] += d
        b[3] += c
    return by_month


def fetch(lat, lon):
    qs = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "start_date": START,
            "end_date": END,
            "hourly": "cloud_cover,temperature_2m,relative_humidity_2m,wind_speed_10m,dew_point_2m",
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        }
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(f"{ARCHIVE}?{qs}", timeout=60) as r:
                return json.loads(r.read())["hourly"]
        except Exception as exc:
            wait = 5 * (attempt + 1)
            print(f"  retry {attempt + 1} after {wait}s ({exc})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"failed to fetch {lat},{lon}")


def main():
    grid = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/grid_scot.json"))
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else len(grid)
    out = []
    for n, site in enumerate(grid[:limit]):
        bm = score_point(site["lat"], site["lon"], fetch(site["lat"], site["lon"]))
        for month, (cnt, sq, sd, sc) in bm.items():
            out.append(
                {
                    "site_id": site["id"],
                    "month": month,
                    "window_count": cnt,
                    "sum_q": round(sq, 3),
                    "sum_darkness": round(sd, 3),
                    "sum_clarity": round(sc, 3),
                }
            )
        if n % 20 == 0:
            print(f"  {n + 1}/{limit} points", file=sys.stderr)
        time.sleep(1.0)  # be polite to Open-Meteo
    json.dump(out, open("/tmp/climatology.json", "w"))
    print(f"{limit} points -> {len(out)} (site,month) rows -> /tmp/climatology.json")


if __name__ == "__main__":
    main()
