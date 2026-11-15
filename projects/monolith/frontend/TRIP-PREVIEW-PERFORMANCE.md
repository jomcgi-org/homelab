# Trip routes render before the map loads

The summary and day pages previously server-rendered empty map containers.
They now render an SVG route preview from the GPS points already in page data.
The full MapLibre route, photos and navigation still use the complete data.
The preview remains visible until a render event confirms that the basemap and
every route source have loaded without a critical request failure. A failure
restores the preview and offers an explicit retry. Retry reloads the style and
rebuilds the complete route sources while retaining the selected photo and day
map camera. Optional hillshade errors leave the working basemap visible.

MapLibre 6.9.0 converts vector HTTP 404s into loaded empty tiles, and raster
expiry reloads can fail without a loading event or another frame. Each map uses
MapLibre's documented `addProtocol` and `transformRequest` APIs to observe the
actual tile HTTP result. A missing basemap tile or network failure retains or
restores the complete route preview and Retry. This policy is deliberately
conservative: a failure keeps the preview even if a parent tile might provide
fallback coverage. It does not infer success from empty rendered features or
inspect private tile state. Valid empty HTTP 200 and 204 responses still work.

The protocol fetches the original HTTPS URL once, passes through body bytes,
request headers, credentials, cache mode and abort signals, and returns cache
control, expiry and ETag metadata. It adds no cache, polling or automatic retry.
Each map owns its protocol registration. Retry and context loss cancel pending
requests and advance a generation so late failures cannot affect the new style.
Unmounting aborts pending requests and removes the registration. The optional
terrain URL retains MapLibre's normal request path and nonblocking error policy.

The public styles checked on 2026-11-15 contain OpenFreeMap vector and raster
sources for the summary map and a Carto vector source for the day map. The
protocol moves worker tile fetches through the main thread. Actual HTTPS URLs
and HTTP cache keys stay unchanged, but page-scoped CDP throttling may now apply
to requests that previously ran in workers. Worker throttling in earlier hosted
runs was unverified. New hosted measurements must record this profile difference;
request-time differences alone do not establish a production speedup.

During WebGL context loss, Retry queues a reload until the browser reports
context restoration. The Retry button stays visible until a successful render.
MapLibre restores a serialized style containing the custom route sources and
layers, so style initialization reuses existing IDs. The tests reject duplicate
IDs and replay this serialized-style lifecycle, with and without a queued retry.
They do not claim WebGL canvas validation; hosted browser verification is still
required.

The full preview blocks clicks on hidden geometry; the map's zoom and
attribution controls stay above it. Days without GPS points show a compact
caption after the basemap is ready, leaving the working map available.

The preview uses Web Mercator with equal horizontal and vertical scale,
north at the top and adjacent longitudes unwrapped across the date line.
Each day contributes at most 128 sampled points, including both endpoints.
Bounds include every valid GPS point before sampling. The caption identifies
the drawing as a route preview and reports the complete GPS point count.
Routes beyond 32 days, polar routes and journeys spanning more than one world
width show a data summary. The interactive map retains their complete data.

## Measured HTML cost

Measured on 2026-11-14 and 2026-11-15 with Node 24.22.1, Svelte's server renderer and the public
trip snapshot containing 4,838 points across nine days. The first day has 288
points. The baseline is `1f536d94934ccfef73aa3a2129f00be60f8a44ca`.
Each row uses three consecutive component renders after grouping the input
outside the timed region. Gzip sizes use Node's default `gzipSync`.

| Map       | SVG paths before / after | HTML bytes before / after | Gzip bytes before / after | Median render ms before / after |
| --------- | ------------------------ | ------------------------- | ------------------------- | ------------------------------- |
| Summary   | 0 / 9                    | 54 / 15,393               | 74 / 4,867                | 0.092 / 3.074                   |
| First day | 0 / 1                    | 73 / 2,110                | 93 / 1,025                | 0.100 / 1.331                   |

Raw render times in milliseconds:

| Map       | Before                       | After                        |
| --------- | ---------------------------- | ---------------------------- |
| Summary   | 0.950506, 0.091717, 0.044596 | 9.769185, 2.149573, 3.074080 |
| First day | 0.589484, 0.100231, 0.037837 | 1.874712, 1.331316, 1.215392 |

These are local component costs, excluding shared styles, page data and network
requests. They show the added HTML and CPU cost of replacing the empty first
view. Mobile LCP, CLS and production response times remain unmeasured for this
change until deployment.

To repeat the measurement, capture the public trip response, call `groupByDay`
once with its points and timezone, then render `TripMap` with `{ days }` and
`DayMap` with `{ points: days[0].points }` in the SSR Vitest environment.
Record `performance.now()` around `render(Component, { props }).body` three
times, `Buffer.byteLength(body)` and `gzipSync(body).length` on each revision.
Use the same snapshot on both revisions.

## Regression coverage

The geometry tests cover Mercator latitude spacing, date-line crossings,
bounded samples, endpoints, invalid coordinates and the summary fallbacks.
SSR tests assert real geometry and accessible text without a client fetch.
Composed DOM tests cover basemap readiness, request failures before and after
painting without later source or render events, WebGL context loss, failed
retries, hidden route clicks, stale request cancellation, protocol teardown,
and preserving source data, the selected photo and camera across retries.

Protocol contract tests execute the installed VectorTileSource and
VectorTileWorkerSource through the registered protocol for a 404, a valid empty
200 and a subsequent expiry 404. Another test runs the real TileManager
`_reloadTile` and RasterTileSource request path: a silent raster expiry 404
triggers recovery without injecting a loading event or frame. Tests also cover
the real empty raster image response path, unchanged successful bytes and cache
metadata, one fetch per request, network errors, aborts, late responses,
independent map registrations and optional terrain bypass.

Direct day navigation restores the preview until the replacement source
renders. A day without GPS points keeps a compact caption while nearby photo
loading still proceeds after readiness. Existing trip tests retain photo
selection and route navigation assertions.

The tests are included by the existing `src/**/*.test.js` BUILD glob. The trip
suite passes 135 tests across 12 files, and the trip fixture production build
passes. Run from the frontend directory:

```sh
node node_modules/vitest/vitest.mjs run src/routes/public/app/trips src/lib/trips src/lib/public/components/trips/route-preview.ssr.test.js
node node_modules/vite/bin/vite.js build --config trips-preview/vite.config.js
```

This change builds on the photo loading and day navigation change in
[#7974](https://github.com/jomcgi-org/homelab/pull/7974). DayMap uses one reactive
readiness state for its preview and the existing photo callback. Route changes and
cleanup reset it. Empty route sources still signal photo readiness after
rendering; the compact no-GPS caption stays visible independently.
