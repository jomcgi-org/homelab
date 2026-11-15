# Stars performance

`/app/stars` receives the forecast site snapshot in its server load. Map-only
and historical layers have separate same-origin endpoints.

The landed [server-rendered snapshot change](https://github.com/jomcgi-org/homelab/pull/7972)
shows real data rows while the interactive map loads. Production observations
and served-generation limits are recorded in the
[measurement report](../../../../../docs/public-app-performance-2026-11-15.md).
Cache and bundle limits alone do not establish mobile performance.

## Forecast payload

The server loader shares identical `best_hours` records within one response
before SvelteKit serializes it. The serializer preserves those references, so
repeated records occupy one definition in the inline data. The complete JSON
value is the identity: no fields, hours or sites are removed, including unknown
fields from a newer backend. The pool is local to the load; it does not retain
forecasts between requests or change the cache windows below.

Forecast records are read-only inputs. `liveWindows` creates a filtered array;
the map groups and sorts new card arrays. Neither the map nor the night picker
writes to an hour. Site rows and their `best_hours` arrays remain separate, so
selecting or filtering one site does not alter another site's array. A future
feature that edits a forecast record must copy it before writing.

### Local serialization measurements

Measured on 2026-11-15 with Node v24.22.1 against base
`280738ca569e3348fa85a7bbac6423aedcb9e4e9`. Three fresh processes per variant
used the same saved public snapshot: 14,234 sites, 94,586 hour records and 532
distinct complete hour values. The full snapshot's JSON SHA-256 stayed
`0079d0850914273f57e8deab2b90cb72914098b3e32ad27d0d35052028c985ed`
before sharing and after evaluating the serialized data in every run.

| Measurement                                               |    Before | Shared hours |
| --------------------------------------------------------- | --------: | -----------: |
| Inline data, decoded bytes                                | 9,271,811 |    3,093,449 |
| Inline data, Brotli quality 5 bytes                       |   180,694 |      150,489 |
| Inline data, gzip level 6 bytes                           |   297,029 |      201,985 |
| Navigation data, decoded bytes                            | 7,881,498 |    3,188,909 |
| Sharing plus HTML serialization, median ms                |    342.46 |       155.77 |
| Sharing alone, median ms                                  |         0 |        50.37 |
| HTML serialization alone, median ms                       |    342.45 |       101.47 |
| Serialized data script compile plus evaluation, median ms |    480.52 |       115.15 |
| Evaluated data heap, median MiB                           |      9.26 |         3.52 |
| Entire benchmark peak RSS, median MiB                     |    444.36 |       228.08 |

The three sharing-plus-serialization samples were 526.52, 304.58 and 342.46 ms
before, then 155.77, 151.76 and 170.77 ms with sharing. Script compilation plus
evaluation took 472.20, 480.52 and 667.14 ms before, then 118.86, 104.55 and
115.15 ms. Input JSON parsing was unchanged code and varied from 43.87 to
201.87 ms on this shared host; these are local CPU observations, not mobile or
production latency claims.

The benchmark calls the installed SvelteKit dependency's `devalue.uneval` for
the page data envelope and `devalue.stringify` for navigation data. Sharing and
HTML serialization are timed together without intervening checks or forced
garbage collection. Compression uses the same Brotli/gzip settings as
`hooks.server.js`. The script compile/evaluate measurement uses a fresh V8
context; it does not include Svelte hydration or DOM work. Evaluated heap is
the difference after forced garbage collection before and after evaluation.
Peak RSS includes the entire benchmark, both serializers, equality checks,
compression and evaluation; it is not a production pod memory measurement.

The backend read and initial JSON parse remain unchanged. Release `0.792.43`
was verified healthy, and fresh production HTML retained all 103,525 captured
hour slots while reducing decoded HTML from 9,894,254 to 3,126,694 bytes with
identical normalized snapshot values. This separate capture has a different
forecast corpus from the local benchmark above. The
[measurement report](../../../../../docs/public-app-performance-2026-11-15.md)
separates that deployed payload check from browser timings and stale edge copies.

### Regression coverage

The existing frontend `:src` and `:src_public` globs include the server helper;
`:test_lib` includes its tests and the changed loader, SSR and DOM tests.
The targeted run passed 227 tests across seven files:

```sh
cd projects/monolith/frontend
corepack pnpm exec vitest run src/lib/server/stars-payload.test.js src/routes/public/app/stars src/lib/public/stars src/public-cache.test.js
```

Tests cover complete JSON equality, differing and future hour fields, separate
requests, frozen input, separate site arrays, loader integration, identical
first-view HTML and opening cards for two sites that share frozen hour records.

## Full history HTTP encoding

`/api/stars/history`, exposed at `/app/stars/history`, encodes the builder's
JSON primitives directly through `JSONResponse`. This avoids FastAPI's second
recursive walk of the completed site and month arrays. `get_history` still
returns a dictionary to the materializer, and its queries, ordering, ETag and
conditional-response logic are unchanged. The map's `/history-map` request
continues to read its existing materialized object.

On 2026-11-15, three fresh Python processes per version measured the actual
FastAPI route against a local, file-backed SQLite fixture. Base revision:
`cc85e454b715ecc32412e11ef8e6035fc3b39e1d`.

| Measurement       |    Before | HTTP wrapper |
| ----------------- | --------: | -----------: |
| Median route time |  1,030 ms |       680 ms |
| Response bytes    | 2,513,273 |    2,513,273 |

The raw route samples were 1,097.96, 1,030.01 and 870.82 ms before, then 774.55,
569.07 and 680.47 ms after. Every response had SHA-256
`2c913827bd615af8807e6e32eb765995e99a88aadd4655f383ed646a2d4e14dd`,
the same cache policy and ETag `"v3-14234-20216951-207269252"`. Separate
conditional requests in each process returned empty 304 responses with the
same ETag and cache headers.

The fixture reconstructs 14,234 sites and all 170,808 monthly rows from the
captured public history response, including zero months. Subsequent local
PostgreSQL 16.14 plans used the migrated schema and reconstructed fixture:
three read-only runs gave median sequential-scan execution of 1.596 ms for
14,234 site metadata rows and 19.699 ms for 170,808 monthly rows. Zero-month
rows are synthetic. These [saved plans](../../../../../docs/public-app-performance-2026-11-15.md#evidence-archive)
do not measure production statistics, concurrency, Python assembly or transport,
and do not justify an index.

After release `0.792.55` was verified healthy, 20 public MISS requests measured
p50/p95 of 1,327.0/1,633.8 ms, compared with 2,077.1/4,231.8 ms in the preceding
hosted series. Both returned 2,513,273 decoded bytes. The runner and network
differed; the lower observed times do not isolate the code change and remain
above the 300 ms target. All 20 normal-key requests were HITs, with p95 42.0 ms.
The [measurement report](../../../../../docs/public-app-performance-2026-11-15.md)
retains the deployment and measurement context. These HTTP timings do not
measure the Historical button interaction.

The registered `//projects/monolith:stars_router_test` covers UTF-8 and escaped
HTTP bytes, content length, cache and ETag headers, empty 304 responses,
climatology changes, skipping the recursive encoder and the direct dictionary
contract used by the materializer. Existing materialization tests still publish
the real history builder's output. Router and model tests pass together: 50
tests. The recursive-encoder regression failed against the original route
before the wrapper was added.

## Historical map transfer

The Historical mode sends the same complete point and polygon collections to
MapLibre through its public [`setData(URL)` API](https://maplibre.org/maplibre-gl-js/docs/API/classes/GeoJSONSource/).
The URL contains base64 JSON generated locally. Worker Fetch can decode and
parse it without the click thread recursively copying every GeoJSON object.
The live layer keeps its existing object input. There is no extra HTTP request,
worker customization, removed feature or geometry simplification.

JSON escaping before `btoa` preserves Unicode IDs and lone surrogates. Base64
contains no `://`, so even an ID containing an HTTPS URL cannot trigger
MapLibre 6.9's custom-protocol delegation to the main thread. This distinction
matters: plain `blob:https://` URLs take that delegation path in this version.

### Hosted evidence before transport changes

The separate [diagnostic run](https://github.com/jomcgi-org/homelab/actions/runs/43665083851)
used Chromium 151.0.7922.34, 4x page CPU slowdown and three fresh contexts. Each
waited for the natural history-map response to complete and two animation
frames before clicking. All three had one document request and finished in
loaded Historical mode.

| Diagnostic observation, ms                | Run 1 | Run 2 | Run 3 |
| ----------------------------------------- | ----: | ----: | ----: |
| Click Event Timing duration               |   440 |   464 |   424 |
| Click processing duration                 | 426.0 | 441.1 | 408.4 |
| Input delay                               |   8.1 |   8.9 |   8.3 |
| Sample weight under GeoJSON `sendAsync`   | 328.1 | 324.8 | 312.7 |
| Recursive transfer serializer self weight | 179.1 | 180.2 | 167.8 |

The last two rows include only stacks under `_dispatchWorkerUpdate`, excluding
tile-load messages. The archived MapLibre asset `BSile_g5.js` identifies the
recursive serializer as `zs` and the enclosing actor's Promise executor.
Sampling weights approximate attribution, not exact function durations. The
profiles include subsequent frames and a one-second delivery window. Their
CDP clock is not aligned exactly with document Event Timing. Large `(program)`
spans remain unattributed, and workers are neither profiled nor certified under
the page CPU throttle. These diagnostic runs never replace normal benchmark
medians or field INP.

### Local transport comparison

Three fresh Vitest processes generated both collections through the real
component from the saved 14,234-site history. Each warmed the installed
MapLibre serializer with the initial live collections, then ran three
alternating object/URL pairs. The measured section includes the actual
`web_worker_transfer.serialize` and native `structuredClone`; the URL path
also includes the production JSON/base64 helper. It does not measure a browser,
worker execution, rendering, CPU throttling or the worker's return message.

| Local measurement, ms                        | Object input | Data URL |
| -------------------------------------------- | -----------: | -------: |
| First sample, median across fresh processes  |       274.21 |   112.12 |
| Median of each process's three-sample median |       219.25 |   112.12 |

The first samples were 246.07, 386.86 and 274.21 ms for objects, and 115.01,
112.12 and 90.81 ms for URLs. Decoded value equality held after every transfer:
14,234 points (2,312,564 JSON bytes) and 14,234 polygons (3,724,686 JSON bytes),
including every ID, coordinate, property and feature's position in the array.

Base64 creates larger temporary strings. MapLibre also returns the parsed
collection from its worker and restores the source's data for query APIs;
that later main-thread work is retained. A verified rollout must remeasure
clicks, subsequent long tasks and map readiness before claiming a live speedup.

The registered frontend tests cover Unicode and empty collections, real data-
URL Fetch, the installed `GeoJSONSource` and `GeoJSONWorkerSource` path without
a main-thread resource bounce, source query data, tile indexing and rapid
updates. The composed map test checks all months, both source types, full
point coordinates and card selection. All 89 Stars tests across seven files
pass. Initial SSR content and all cache windows remain covered by their existing
tests.

## Cache windows

| Responses                     |        TTL |     SWR |    Normal maximum |     SIE |   Error allowance |
| ----------------------------- | ---------: | ------: | ----------------: | ------: | ----------------: |
| HTML, navigation data, `/map` |      1,800 |   3,600 |       5,400 (90m) |  86,400 |   88,200 (24h30m) |
| `/history`, `/history-map`    | 31,536,000 | 604,800 | 32,140,800 (372d) | 604,800 | 32,140,800 (372d) |

Browsers revalidate (`max-age=0`). The loader and proxies mirror
`_SITES_CACHE_CONTROL` and `_HISTORY_CACHE_CONTROL` in the
[backend router](../../../../../../stars/router.py).

The [refresh command](../../../../../../app/jobs_main.py) runs
forecast refresh then [materialization](../../../../../../stars/materialize.py)
every three hours. Failed site fetches retain earlier rows. The hourly prune
updates the database; it does not republish the materialized object. The public
read serves that object without a maximum object-age rejection. Source age
therefore includes upstream forecasts and time since successful publication.

Historical climatology changes through a manual reload. Existing comments
require invalidating `/app/stars/history*` after a reload or response-shape
change. Any account-level purge remains an operator decision; this work makes
no dashboard change. Maximum acceptable forecast age, historical invalidation
and outage windows remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route        | Initial | Deferred | External worker |
| ------------ | ------: | -------: | --------------: |
| `/app/stars` |  85,000 |  330,000 |         170,000 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
