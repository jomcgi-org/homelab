"""Offline ERA5 climatology backfill for the stars historical heatmap (stars v2).

Fetches ~5yr of hourly ERA5 (Open-Meteo archive) per grid point and counts
clear-dark hours by month-of-year into climatology.json. A dark hour is one
where the sun is below -12 deg (nautical/astronomical); a clear-dark hour is a
dark hour whose cloud cover is under 10%. Per (site_id, month-of-year) we emit
two sufficient stats: dark_hours (the denominator) and clear_dark_hours (the
headline metric). Clarity rate downstream = clear_dark_hours / dark_hours.

Output rows: {site_id, month, dark_hours, clear_dark_hours}.

Resumable + hourly-rate-limit aware: saves after every point, skips already-done
sites on restart, and sleeps until the next hour when Open-Meteo returns 429
(its limit is hourly), so a single run self-paces over a few hours to cover all
points. Pure stdlib: NOAA solar elevation (so the dark-hour test matches astral
within tolerance). The -12 deg / 10% thresholds are replicated from
projects/monolith/stars/scoring.py (KEEP IN SYNC with is_clear_dark_hour).
Output is uploaded to SeaweedFS (s3://stars/climatology.json) and ingested by
the stars.load_climatology job.
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
# KEEP IN SYNC with projects/monolith/stars/scoring.py (is_clear_dark_hour):
# a dark hour is sun < -12 deg, a clear-dark hour is dark AND cloud < 10%.
NAUTICAL_DARK_DEG = -12.0
CLEAR_CLOUD_MAX_PCT = 10.0
OUT = "/tmp/climatology.json"

# Adaptive 429 backoff, persisted across points. Open-Meteo's short-window
# (minutely) budget refills in ~1 min, so a 429 from a normal burst clears with
# a short sleep; only a real hourly/daily cap produces 429s that persist across
# retries, which escalates the wait toward RL_MAX. Reset to RL_MIN on any
# success (see fetch), so the common case never pays more than RL_MIN.
RL_MIN, RL_MAX = 60, 1800
_RL = {"wait": RL_MIN}


def sun_elevation_deg(lat, lon, dt):
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
    tst = hour * 60.0 + eqtime + 4.0 * lon
    ha = math.radians(tst / 4.0 - 180.0)
    latr = math.radians(lat)
    cz = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(
        ha
    )
    return 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cz))))


def score_point(lat, lon, hourly):
    """Count clear-dark hours per month-of-year for one grid point.

    Walks every hourly sample, keeps the dark ones (sun < -12 deg), and tallies
    dark_hours plus clear_dark_hours (dark AND cloud < 10%). Returns
    {month: [dark_hours, clear_dark_hours]}. Thresholds KEEP IN SYNC with
    scoring.py is_clear_dark_hour.
    """
    times, cc = hourly["time"], hourly["cloud_cover"]
    by_month = {}
    for i, t in enumerate(times):
        if cc[i] is None:
            continue
        dt = datetime.fromisoformat(t)
        if sun_elevation_deg(lat, lon, dt) >= NAUTICAL_DARK_DEG:
            continue  # not a dark hour: skip
        b = by_month.setdefault(dt.month, [0, 0])
        b[0] += 1  # dark_hours
        if cc[i] < CLEAR_CLOUD_MAX_PCT:
            b[1] += 1  # clear_dark_hours
    return by_month


def fetch(lat, lon):
    qs = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "start_date": START,
            "end_date": END,
            "hourly": "cloud_cover",
            "timezone": "UTC",
        }
    )
    transient = 0
    while True:
        try:
            with urllib.request.urlopen(f"{ARCHIVE}?{qs}", timeout=60) as r:
                hourly = json.loads(r.read())["hourly"]
            _RL["wait"] = RL_MIN  # success: a normal burst cleared, reset backoff
            return hourly
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # The short-window budget usually refills within RL_MIN; sleep,
                # then double the wait (capped at RL_MAX) so persistent 429s from
                # a real hourly/daily cap escalate instead of hammering. Keep
                # retrying rather than skipping: a 429 is transient, not a bad
                # point. The backoff resets the next time any fetch succeeds.
                wait = _RL["wait"]
                print(f"  429 rate-limited, sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                _RL["wait"] = min(wait * 2, RL_MAX)
                continue
            transient += 1
        # Transient network / response faults (connection, timeout, malformed or
        # truncated JSON, missing "hourly" key): print and skip the point after 4
        # strikes rather than abort the whole resumable run. Narrowed from a bare
        # `except Exception` so genuine bugs still propagate.
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
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
        for month, (dark_hours, clear_dark_hours) in score_point(
            site["lat"], site["lon"], hourly
        ).items():
            out.append(
                {
                    "site_id": site["id"],
                    "month": month,
                    "dark_hours": dark_hours,
                    "clear_dark_hours": clear_dark_hours,
                }
            )
        json.dump(out, open(OUT, "w"))  # incremental save after every point
        if n % 10 == 0:
            print(f"  {n + 1}/{len(todo)} this run", file=sys.stderr)
        time.sleep(2.0)
    print(
        f"DONE: {len(set(r['site_id'] for r in out))} sites -> {len(out)} rows -> {OUT}"
    )


if __name__ == "__main__":
    main()
