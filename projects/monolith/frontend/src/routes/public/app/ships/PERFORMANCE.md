# Ships performance

`/app/ships` loads vessel positions on the server. Track and traffic-density
layers use the same-origin `/track/[mmsi]` and `/heat` proxies.

The landed [server-rendered snapshot change](https://github.com/jomcgi-org/homelab/pull/7972)
shows real data rows while the interactive map loads. Its rollout and mobile
first-view results must be checked separately from the cache and bundle limits.

The pending [vessel animation change](https://github.com/jomcgi-org/homelab/pull/7991)
submits geometry changes only for positions that can still move, at the
existing four-updates-per-second cadence. It preserves the full fleet and
snapshot freshness policy.

## Snapshot response

The snapshot selects its fifteen output fields directly instead of loading
complete position and vessel models. It serializes the resulting JSON primitives
once. The left join, full fleet, nulls, name fallback, timestamps, ETag and cache
policy are unchanged; this adds no server-side snapshot cache or age cutoff.

Local actual-route measurements used three interleaved fresh Python processes
per version, FastAPI 0.139.0, SQLModel 0.0.37 and SQLAlchemy 2.0.48:

| Request | Before median | After median |
| --- | ---: | ---: |
| First snapshot | 480.9 ms | 156.3 ms |
| Matching ETag (304) | 288.3 ms | 114.0 ms |

The file-backed SQLite fixture reconstructs all 7,786 captured public positions
and 5,684 vessel metadata rows. It preserves each response field and defaults
unused metadata; it is not a production database copy. All six responses have
identical headers and 2,926,063 body bytes, SHA-256
`730111f6c93162c0f0f50c78388411561099efaaa7e94b32e60323053bfab806`.
The public source capture has SHA-256
`40ee85124646917c6afb759604a5757e925c3161e68197be2018aa152ded4c53`.
The 304 request follows the first request in each process. Neither query promises
row order. These timings isolate local route work; they do not establish
PostgreSQL, production network or mobile latency. Verify the deployed route
separately before claiming a live improvement.

Local PostgreSQL 16.14 `EXPLAIN (ANALYZE, BUFFERS)` on the same reconstructed
rows and migrated schema used a hash join in both versions: median execution
3.417 ms before and 3.624 ms after, across three read-only runs. This does not
show a faster database plan; the measured route saving is in Python model
hydration and JSON encoding. No index or production database change was made.

## Cache windows

| Responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | ---: | ---: |
| HTML and navigation snapshot | 120 | 600 | 720 (12m) | 86,400 | 86,520 (24h2m) |
| Vessel track | 60 | 300 | 360 (6m) | 86,400 | 86,460 (24h1m) |
| Traffic heat | 300 | 3,600 | 3,900 (65m) | 86,400 | 86,700 (24h5m) |

All three policies set browser `max-age=0`. The loader and proxies mirror the
constants in the [backend router](../../../../../../ships/router.py).

[AIS ingestion](../../../../../../ships/ingest.py) consumes a
continuous feed and flushes at 200 rows or when an incoming message finds the
batch older than two seconds. This is not a guaranteed reporting cadence for
each vessel. The snapshot query has no timestamp cutoff on `latest_positions`;
a vessel without new accepted fixes can remain older than every cache window.
The browser's two-minute refresh and five-minute dead-reckoning clamp do not
bound that source age.

The heat rollup runs hourly and combines recent traffic with banked historical
cells. Heat is a density history, not a live-position layer. The acceptable
source age and outage behavior remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Heat request cost

The backend reads `lat_bin`, `lon_bin` and `count` directly from each rollup
table. It sums overlapping cells with the existing merge and colour-stop
functions, then returns `JSONResponse`. This avoids hydrating an ORM object and
unused timestamp per row, and avoids FastAPI recursively copying the completed
grid before JSON encoding. Both rollup tables are still read on every request;
the hourly job and cache windows are unchanged.

The November 15, 2026 hosted probe recorded uncached `/app/ships/heat` p50 of
5,281 ms and p95 of 6,473 ms. Its ordinary cached requests met the 100 ms API
target. These are measurements before this backend change.

Three fresh Python processes per version measured the real FastAPI route with
a local, file-backed SQLite fixture:

| Measurement | Before | Scalar reads and direct JSON |
| --- | ---: | ---: |
| Median route time | 3,339 ms | 626 ms |
| Median process peak RSS | 269.8 MiB | 120.7 MiB |
| Response bytes | 2,386,369 | 2,386,369 |

The fixture uses the captured public grid's 145,247 cells. Its split into
10,000 live rows and 145,020 historical rows is synthetic; the public response
does not disclose that split. Every measured response matched the captured
JSON bytes and cache headers. Neither SQL query specifies `ORDER BY`, so this
local byte equality does not guarantee row order across PostgreSQL query plans.
Cell coordinates, summed counts and colour stops retain their existing meaning.

A separate stage profile located most local cost in historical ORM hydration
(2,579 ms) and FastAPI's recursive encoder (443 ms). These SQLite and Python
results do not measure PostgreSQL, the SvelteKit proxy or production network
latency. No local PostgreSQL instance was available for `EXPLAIN ANALYZE`.
Production rollout and uncached remeasurement remain required before claiming
a live speedup.

The endpoint regressions run in `//projects/monolith:ships_router_test` and cover
exact HTTP JSON, cache headers, selected SQL columns, direct encoding and reading
updated rollup data on the next request.

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/ships` | 80,000 | 330,000 | 170,000 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
