"""Offline ERA5 climatology backfill for the stars historical heatmap (ADR 009).

Fetches ~5yr of hourly ERA5 (Open-Meteo archive) per grid point, scores each
dark hour with the same Q = D x C x W model as the live pipeline, and aggregates
sufficient statistics by month-of-year into climatology.json.

Resumable + hourly-rate-limit aware: saves after every point, skips already-done
sites on restart, and sleeps until the next hour when Open-Meteo returns 429
(its limit is hourly), so a single run self-paces over a few hours to cover all
points. Pure stdlib: NOAA solar elevation (so D matches astral within tolerance)
and the Q-factor formulas replicated from projects/monolith/stars/scoring.py
(KEEP IN SYNC). Output is uploaded to SeaweedFS (s3://stars/climatology.json) and
ingested by the stars.load_climatology job.
"""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
START, END = "2021-01-01", "2025-12-31"
CIVIL, ASTRO, CLOUD_SPAN = -6.0, -18.0, 45.0
OUT = "/tmp/climatology.json"


def sun_elevation_deg(lat, lon, dt):
    doy = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60.0
    g = 2 * math.pi / 365.0 * (doy - 1 + (hour - 12) / 24.0)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    )
    tst = hour * 60.0 + eqtime + 4.0 * lon
    ha = math.radians(tst / 4.0 - 180.0)
    latr = math.radians(lat)
    cz = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cz))))


def darkness_factor(e):
    if e >= CIVIL:
        return 0.0
    if e <= ASTRO:
        return 1.0
    return (CIVIL - e) / (CIVIL - ASTRO)


def cloud_factor(cloud, d):
    return max(0.0, 1.0 - max(0.0, cloud - (5.0 + 5.0 * d)) / CLOUD_SPAN)


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
    avg = (_humidity_score(humidity) + 100.0 + _wind_score(wind) + _dew_score(dew_spread)) / 4.0
    return 0.7 + 0.3 * (avg / 100.0)


def score_point(lat, lon, hourly):
    times, cc, temp = hourly["time"], hourly["cloud_cover"], hourly["temperature_2m"]
    rh, wind, dew = hourly["relative_humidity_2m"], hourly["wind_speed_10m"], hourly["dew_point_2m"]
    by_month = {}
    for i, t in enumerate(times):
        if cc[i] is None or temp[i] is None or dew[i] is None:
            continue
        dt = datetime.fromisoformat(t)
        d = darkness_factor(sun_elevation_deg(lat, lon, dt))
        if d <= 0:
            continue
        c = cloud_factor(cc[i], d)
        q = d * c * weather_modifier(rh[i] or 100, wind[i] or 0, temp[i] - dew[i]) * 100.0
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
            "latitude": lat, "longitude": lon, "start_date": START, "end_date": END,
            "hourly": "cloud_cover,temperature_2m,relative_humidity_2m,wind_speed_10m,dew_point_2m",
            "timezone": "UTC", "wind_speed_unit": "ms",
        }
    )
    transient = 0
    while True:
        try:
            with urllib.request.urlopen(f"{ARCHIVE}?{qs}", timeout=60) as r:
                return json.loads(r.read())["hourly"]
        except urllib.error.HTTPError as e:
            if e.code == 429:  # hourly limit: wait for the next hour and resume
                print("  429 hourly limit, sleeping 1h...", file=sys.stderr)
                time.sleep(3700)
                continue
            transient += 1
        except Exception as exc:
            transient += 1
            print(f"  transient error: {exc}", file=sys.stderr)
        if transient >= 4:
            return None  # skip this point, do not abort the run
        time.sleep(5 * transient)


def main():
    grid = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/grid_scot.json"))
    out = json.load(open(OUT)) if os.path.exists(OUT) else []
    done = {r["site_id"] for r in out}
    todo = [s for s in grid if s["id"] not in done]
    print(f"{len(done)} sites done, {len(todo)} to go", file=sys.stderr)
    for n, site in enumerate(todo):
        hourly = fetch(site["lat"], site["lon"])
        if hourly is None:
            print(f"  skipped {site['id']}", file=sys.stderr)
            continue
        for month, (cnt, sq, sd, sc) in score_point(site["lat"], site["lon"], hourly).items():
            out.append(
                {
                    "site_id": site["id"], "month": month, "window_count": cnt,
                    "sum_q": round(sq, 3), "sum_darkness": round(sd, 3), "sum_clarity": round(sc, 3),
                }
            )
        json.dump(out, open(OUT, "w"))  # incremental save after every point
        if n % 10 == 0:
            print(f"  {n + 1}/{len(todo)} this run", file=sys.stderr)
        time.sleep(2.0)
    print(f"DONE: {len(set(r['site_id'] for r in out))} sites -> {len(out)} rows -> {OUT}")


if __name__ == "__main__":
    main()
