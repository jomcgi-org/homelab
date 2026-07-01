"""Offline CERRA clear-dark-hours backfill for the stars historical layer.

This supersedes the per-site Open-Meteo loop in backfill_climatology.py (which
was rate-limited to ~4 days for the 14k-site grid). Instead we download ONE bulk
CERRA cloud-cover cube and sample it locally at every grid site, no rate limit,
runs in minutes. CERRA is the Copernicus regional reanalysis: ~5.5 km total
cloud cover, 3-hourly, night-valid (a model field, day/night symmetric), so it
resolves Scottish mesoscale microclimates far better than 31 km ERA5 (a glen
that is statistically clear vs a cloudy one 6 km away).

This is NOT runtime code (grid_gen/** is excluded from the monolith image). Run
it by hand to (re)generate climatology.json, then upload + load it.

Pipeline:
  1. Open the CERRA GRIB cube(s) (xarray + cfgrib), slice to a Scotland bbox.
  2. Build a KDTree (3D unit-sphere coords, lon-wrap safe) over the projected
     2D lat/lon grid so each site maps to its nearest ~5.5 km cloud cell.
  3. Reindex the 3-hourly cloud series to hourly (nearest) so darkness, which
     moves fast, is counted at full hourly resolution while cloud stays at its
     native 3-hourly cadence.
  4. Per site, per month-of-year: count dark hours (sun < -12 deg, NOAA solar
     elevation) and clear-dark hours (dark AND cloud < 10%).
  5. Emit climatology.json {site_id, month, dark_hours, clear_dark_hours},
     matching stars.load_climatology / grid._load_climatology_sync.

KEEP IN SYNC with projects/monolith/stars/scoring.py: dark = sun_elevation_deg
< NAUTICAL_DARK_DEG (-12.0); clear-dark also requires cloud < CLEAR_CLOUD_MAX_PCT
(10.0). Strict < on both.

Reproduction runbook:

  # 1. Deps (the offline geo stack; cfgrib needs the eccodes binary):
  python3 -m venv /tmp/geoenv && /tmp/geoenv/bin/pip install \
      cdsapi xarray cfgrib dask scipy numpy
  brew install eccodes          # or: conda install -c conda-forge eccodes

  # 2. Copernicus CDS account (free) + new-CDS token in ~/.cdsapirc:
  #   url: https://cds.climate.copernicus.eu/api
  #   key: <personal access token from cds.climate.copernicus.eu profile>
  # Accept the CERRA licence once at the dataset's "Manage licences" tab.

  # 3. Download ~5 yr of total cloud cover, night-only timesteps (09/12/15Z are
  #    never astronomically dark in Scotland), per-year GRIB (~3.9 GB/year, the
  #    full European domain; CERRA's projected grid has no server-side bbox).
  #    reanalysis-cerra-single-levels, variable total_cloud_cover,
  #    level_type surface_or_atmosphere, product_type analysis,
  #    time [18,21,00,03,06], all months/days, data_format grib.

  # 4. Process + upload + load:
  /tmp/geoenv/bin/python backfill_cerra.py \
      --grib '/tmp/cerra_tcc_*.grib' --grid /tmp/grid.json \
      --output /tmp/climatology.json
  kubectl port-forward -n seaweedfs svc/seaweedfs-s3 8333:8333 &
  curl -X PUT -T /tmp/climatology.json http://localhost:8333/stars/climatology.json
  homelab scheduler jobs run-now stars.load_climatology

  # 5. Invalidate the CDN. /api/stars/history* is cached at the edge for a year
  #    (immutable between reloads), so PURGE Cloudflare for the history paths
  #    right after the load or the new climatology will not show until expiry.
  #    Do this every reload (and after any /history response-shape change):
  #    Cloudflare dashboard -> Caching -> Purge -> "https://jomcgi.dev/app/stars/history*"
  #    (or the API: POST .../zones/<zone>/purge_cache with that prefix).
"""

import argparse
import glob
import json
import math

import numpy as np
import xarray as xr

NAUTICAL_DARK_DEG = -12.0
CLEAR_CLOUD_MAX_PCT = 10.0
# Scotland bbox (lat min/max, lon min/max) to crop the European CERRA domain.
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 54.3, 61.2, -8.5, -0.5


def _to_xyz(lat, lon):
    """Lat/lon (deg) to 3D unit-sphere coords, so KDTree distance is great-circle
    and lon-wrap / latitude distortion never bite."""
    la, lo = np.deg2rad(lat), np.deg2rad(lon)
    return np.stack(
        [np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=-1
    )


def sun_elevation_deg(lat, lon, times):
    """Vectorized NOAA solar elevation (deg) for one site over an array of UTC
    datetimes. Same algorithm as the Open-Meteo backfill, broadcast over time."""
    t = np.asarray(times, dtype="datetime64[s]")
    year_start = t.astype("datetime64[Y]")
    doy = (t.astype("datetime64[D]") - year_start.astype("datetime64[D]")).astype(
        int
    ) + 1
    secs = (t - t.astype("datetime64[D]")).astype("timedelta64[s]").astype(float)
    hour = secs / 3600.0
    g = 2.0 * math.pi / 365.0 * (doy - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * np.cos(g)
        - 0.032077 * np.sin(g)
        - 0.014615 * np.cos(2 * g)
        - 0.040849 * np.sin(2 * g)
    )
    decl = (
        0.006918
        - 0.399912 * np.cos(g)
        + 0.070257 * np.sin(g)
        - 0.006758 * np.cos(2 * g)
        + 0.000907 * np.sin(2 * g)
        - 0.002697 * np.cos(3 * g)
        + 0.00148 * np.sin(3 * g)
    )
    tst = hour * 60.0 + eqtime + 4.0 * lon
    ha = np.deg2rad(tst / 4.0 - 180.0)
    latr = math.radians(lat)
    cz = math.sin(latr) * np.sin(decl) + math.cos(latr) * np.cos(decl) * np.cos(ha)
    return 90.0 - np.degrees(np.arccos(np.clip(cz, -1.0, 1.0)))


def load_cube(grib_glob):
    """Open + concat the CERRA GRIB file(s), crop to the Scotland bbox, and
    return (tcc[time, cell], lat[cell], lon[cell], times) over a 1D cell axis
    (Scotland cells only). Slices to the Scotland WINDOW lazily before
    materializing so the full ~41 GB European cube never lands in memory."""
    files = sorted(glob.glob(grib_glob))
    if not files:
        raise SystemExit(f"no GRIB files match {grib_glob}")
    ds = xr.open_mfdataset(files, engine="cfgrib", combine="by_coords")
    lat = ds["latitude"].values
    lon = ((ds["longitude"].values + 180.0) % 360.0) - 180.0
    keep = (lat >= LAT_MIN) & (lat <= LAT_MAX) & (lon >= LON_MIN) & (lon <= LON_MAX)
    rows = np.where(np.any(keep, axis=1))[0]
    cols = np.where(np.any(keep, axis=0))[0]
    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    tcc_win = ds["tcc"].isel(y=slice(y0, y1), x=slice(x0, x1)).values  # (time, Y, X)
    lat_win, lon_win, keep_win = (
        lat[y0:y1, x0:x1],
        lon[y0:y1, x0:x1],
        keep[y0:y1, x0:x1],
    )
    yx = np.argwhere(keep_win)
    cell_tcc = tcc_win[:, yx[:, 0], yx[:, 1]]  # (time, n_cells) Scotland only
    cell_lat = lat_win[keep_win]
    cell_lon = lon_win[keep_win]
    times = ds["valid_time"].values if "valid_time" in ds else ds["time"].values
    return cell_tcc, cell_lat, cell_lon, np.asarray(times, dtype="datetime64[s]")


def main():
    from scipy.spatial import cKDTree

    ap = argparse.ArgumentParser(description="CERRA clear-dark-hours backfill.")
    ap.add_argument("--grib", default="/tmp/cerra_tcc_*.grib")
    ap.add_argument("--grid", default="/tmp/grid.json")
    ap.add_argument("--output", default="/tmp/climatology.json")
    args = ap.parse_args()

    cell_tcc, cell_lat, cell_lon, times = load_cube(args.grib)
    print(f"CERRA: {cell_tcc.shape[1]} Scotland cells, {len(times)} time steps")

    # Reindex the 3-hourly cloud onto an hourly axis (nearest), so darkness is
    # counted hourly while cloud stays at its native cadence.
    t0, t1 = times.min(), times.max()
    hourly = np.arange(t0, t1 + np.timedelta64(1, "h"), np.timedelta64(1, "h"))
    idx = np.clip(np.searchsorted(times, hourly), 1, len(times) - 1)
    pick_left = (hourly - times[idx - 1]) <= (times[idx] - hourly)
    nearest = np.where(pick_left, idx - 1, idx)
    months = hourly.astype("datetime64[M]").astype(int) % 12 + 1

    tree = cKDTree(_to_xyz(cell_lat, cell_lon))

    grid = json.load(open(args.grid))
    out = []
    for n, site in enumerate(grid):
        _, k = tree.query(_to_xyz(np.array([site["lat"]]), np.array([site["lon"]])))
        cloud_hourly = cell_tcc[nearest, int(k[0])]  # (n_hours,) percent
        elev = sun_elevation_deg(site["lat"], site["lon"], hourly)
        dark = elev < NAUTICAL_DARK_DEG
        clear_dark = dark & (cloud_hourly < CLEAR_CLOUD_MAX_PCT)
        d = np.bincount(months[dark], minlength=13)
        c = np.bincount(months[clear_dark], minlength=13)
        for mo in range(1, 13):
            if d[mo] == 0 and c[mo] == 0:
                continue
            out.append(
                {
                    "site_id": site["id"],
                    "month": mo,
                    "dark_hours": int(d[mo]),
                    "clear_dark_hours": int(c[mo]),
                }
            )
        if n % 1000 == 0:
            print(f"  {n}/{len(grid)} sites")

    json.dump(out, open(args.output, "w"))
    sites = len({r["site_id"] for r in out})
    print(f"DONE: {sites} sites -> {len(out)} rows -> {args.output}")


if __name__ == "__main__":
    main()
