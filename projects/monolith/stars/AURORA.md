# Aurora snapshot production

The near-term connector delivery in #7342 validates NOAA OVATION and MET
Locationforecast independently. This additive producer uses the existing stars
bucket and private batch-job writer; the public connector keeps its read-only
credential. No bucket, account, access policy or archival retention is created.

`stars-aurora` is a suspended ten-minute Argo CronWorkflow. It runs the canonical
Go materializer in the existing jobs image, with a 240-second deadline, Forbid
concurrency, disposable scheduling priority and a 512 MiB ceiling. The producer
has a three-minute collection deadline and no HTTP retries or redirects.
Production activation and `config.auroraEnabled` remain separate explicit steps.

The initial inventory is at most 64 real stars grid sites, selected consistently
across occupied half-degree cells. Coordinates sent to MET have four decimal
places. This is limited coverage: the connector still requires a site within
1 km and does not extrapolate a terrain or weather forecast across Scotland.
NOAA's usual horizon is roughly 30 to 90 minutes. Only the actual published
forecast instant is supported; stale data and other instants return unknown.

Each run reads two fixed objects, `aurora-provider-cache-v1.json` and optional
`aurora-terrain-v1.json`, and writes the validated cache and `aurora.json`.
At ten-minute cadence that is at most 288 object reads and 288 writes per day,
excluding the connector's existing refresh reads. Cached objects are replaced,
not accumulated. The cache and materialized snapshot are each capped at 16 MiB.
This bound does not establish remaining account quota or authorize extra cash.
Verify existing storage headroom before unsuspending the job.

NOAA is refreshed independently, at most once per run. MET bodies are reused
until HTTP Expires; absent/expired expiry falls back to 30 minutes. Last-Modified
is sent as If-Modified-Since on the next eligible fetch. A cache hit retains its
original collection clock. A 304 supplies a new verification clock for the same
body, preserving MET's updated_at. Requests are sequential and spaced at least
one second apart. A malformed source, access error, oversized body or invalid
materialization leaves the previous public snapshot untouched. Source clocks
continue to age and the connector returns unknown when they expire.

## Terrain preparation

Missing profiles are absent, never zero-degree horizons. Existing profiles must
match the selected site's exact rounded coordinates. To reproduce a profile
map offline, use `grid_gen/prepare_aurora_terrain.py` with a local source tile,
its reviewed SHA-256, the exact site inventory and the built `stars-aurora`
binary. It neither downloads nor uploads. It crops without resampling and uses
the same 19-ray, 20 km, curvature-adjusted Go algorithm as the connector tests.
Uncovered/nodata cells remain unknown. The profile records each crop's hash.

A bounded source verified during release preparation is the official USGS tile:

- [GMTED2010 mean 7.5 arc-second, 50N030W](https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/topo/downloads/GMTED/Global_tiles_GMTED/075darcsec/mea/W030/50N030W_20101117_gmted_mea075.tif)
- SHA-256 `855ee05a2ff4e094eb87d9faddcba739d3ca85ee1120df96e324ba2ef067de91`
- 276,711,877 bytes, EPSG:4326, nodata -32768, public-domain USGS source.

The source-dependent vertical datum, coarser terrain quality above 60 N and
sampling limitations must stay visible. These profiles do not prove access,
exclude trees/buildings, or guarantee a clear view. Four local validation
coordinates covered the Highlands, mountains, Western Isles and Shetland;
they are not the production site inventory and were not published.

Before public acceptance, verify the merged source's chart publication and
healthy Kargo/Argo rollout, a successful scheduled producer, the loaded snapshot
digest and source clocks, and the public GET/MCP response. Retain unknown where
terrain, site coverage, source validity or freshness is missing. Do not report
that data is live merely because the public method is registered.
