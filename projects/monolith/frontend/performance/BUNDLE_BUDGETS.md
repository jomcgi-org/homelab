# Public app bundle budgets

Both frontend production builds check emitted JavaScript before succeeding.
The existing `//projects/monolith/frontend:build_test` and
`//projects/monolith/frontend:build_test_public` CI targets enforce
`bundle-budgets.json`. A new page under `/public/app` or `/public/scotland`
needs an explicit budget. Missing pages, missing chunks and unrecognized
SvelteKit manifest declarations fail the check.
Each endpoint in the same scope must also be classified explicitly as an API
or a budgeted document. An unclassified `+server.js` fails even when it has no
page component. An API classification never exempts a page on the same route.

Initial bytes include the SvelteKit runtime, router, layouts, page and their
static imports, counted once per route. Deferred bytes include the selected
route's dynamic imports and their dependencies. The router's imports of other
pages are excluded. Worker scripts have a separate budget. Every file is
compressed independently with gzip level 9, matching the units in the JSON.
The check also rejects MapLibre or Mermaid in the initial import closure.

These are regression limits based on measured builds, lowered as reductions
land. They do not establish the mobile targets of LCP below 1.5 seconds, CLS below 0.05 or INP below 200 ms.
They exclude SSR HTML, inline data, CSS, fonts, images, map tiles and API
responses. The production measurement harness records those transfers and
browser timings separately. In particular, the stars and ships inline
snapshots are larger than their JavaScript bundles.

## Measured initial JavaScript

The baseline uses `main` at `1f536d94934ccfef73aa3a2129f00be60f8a44ca` with
the public source exclusions from `:src_public`: private routes, friends
routes and `src/lib/private` are absent. Values are bytes. The budget-only
rebuild changes no client behavior. Its gzip sizes differ by 7 to 16 bytes
because generated version strings and chunk names change. The last column
shows current limits; the Notes reduction is measured below.

| Route | Baseline gzip | Budget-only rebuild gzip | Current initial gzip limit |
| --- | ---: | ---: | ---: |
| `/app/hikes` | 72,096 | 72,085 | 85,000 |
| `/app/ships` | 68,017 | 68,004 | 80,000 |
| `/app/stars` | 72,636 | 72,623 | 85,000 |
| `/app/trips` | 62,834 | 62,825 | 75,000 |
| `/app/trips/[slug]` | 69,684 | 69,677 | 85,000 |
| `/app/trips/[slug]/day/[day]` | 72,402 | 72,394 | 85,000 |
| `/app/trips/[slug]/timeline` | 69,555 | 69,543 | 85,000 |
| `/app/notes` | 91,207 | 91,198 | 75,000 |
| `/app/llm-leaderboard` | 72,534 | 72,526 | 85,000 |
| `/scotland` | 67,463 | 67,454 | 80,000 |
| `/scotland/privacy` | 66,832 | 66,823 | 80,000 |
| `/scotland/support` | 66,831 | 66,822 | 80,000 |
| `/scotland/terms` | 66,833 | 66,824 | 80,000 |
| `/app/grimoire` | 89,693 | 89,679 | 105,000 |
| `/app/grimoire/adventure/[id]` | 73,761 | 73,748 | 90,000 |
| `/app/grimoire/book/[book]` | 79,371 | 79,356 | 95,000 |
| `/app/grimoire/book/[book]/c/[chunk]` | 72,467 | 72,453 | 90,000 |
| `/app/grimoire/chat` | 84,328 | 84,314 | 100,000 |
| `/app/grimoire/chat/s/[id]` | 66,099 | 66,086 | 80,000 |
| `/app/grimoire/entities` | 72,363 | 72,349 | 90,000 |
| `/app/grimoire/entity/[id]` | 77,065 | 77,052 | 95,000 |
| `/app/grimoire/explore` | 72,363 | 72,349 | 90,000 |
| `/app/grimoire/library` | 75,332 | 75,318 | 90,000 |
| `/app/grimoire/world` | 87,302 | 87,286 | 105,000 |

Hikes, ships, stars and trip detail views defer 285,621 gzip bytes of MapLibre,
limited to 330,000 bytes. Hikes, ships and stars also load an external worker
measured at 146,653 gzip bytes, limited to 170,000 bytes. Trip detail views now
use that same emitted worker and limit. The library default previously pointed
to an absent chunk, so the initial zero-worker budget described a broken map.
The other pages have no deferred JavaScript in this baseline. Notes now uses
a 50,000-byte deferred allowance for its interactive graph; its initial limit
still applies independently.

## Trip maps use the emitted worker

On 2026-11-14 at 23:41 UTC, a production GET of
`/_app/immutable/chunks/maplibre-gl-worker.mjs` returned HTTP 404 with
`Content-Type: text/html`. All three captured trip-summary browser runs
reported that failed request. Installed MapLibre 6.9.0 resolves this default
relative to its module URL, so bundling its main module changes the directory
without copying that default file there.

TripMap and DayMap now import the worker with Vite's `?worker&url` and call
`setWorkerUrl` before constructing the map. Timeline's map uses TripMap. The
fixture wrapper previously configured the worker itself and masked the
production failure; it now forwards the same setter used by the components.

Local full production builds before and after the fix use main at
`8c8cac480365930994dbe0c62f42ecc97420d849`. These include private route metadata,
so their initial sizes differ from the public-only table above. Each value is
gzip bytes per file, summed over the route's closure.

| Trip route | Initial before / after | Deferred before / after | Worker before / after |
| --- | ---: | ---: | ---: |
| Index | 64,665 / 64,676 | 0 / 0 | 0 / 0 |
| Summary | 71,575 / 71,709 | 285,621 / 285,621 | 0 / 146,653 |
| Day | 74,284 / 74,418 | 285,621 / 285,621 | 0 / 146,653 |
| Timeline | 71,436 / 71,564 | 285,621 / 285,621 | 0 / 146,653 |

The three map routes reference the same emitted
`/_app/immutable/workers/maplibre-gl-worker-DdG60dM_.js` asset. It is 506,035
uncompressed bytes, within the 170,000-byte gzip worker limit. The built Node
adapter serves that URL with HTTP 200 and `Content-Type: text/javascript`;
its response matches the emitted file. The index retains a zero worker budget.

The constructor-order regression fails for summary, day and timeline before
the fix and passes afterward. The trip and bundle-budget suite passes 101 tests
across 10 files; the production and fixture builds pass. These checks establish
asset delivery locally. After rollout, repeat the production worker GET and
browser map checks before claiming the production failure is resolved.

Grimoire keeps its admission and session behavior. Its bundle budgets do not
require server rendering protected data or permit caching session responses.
`/app/firecracker` is a server redirect. Its generated page closure is still
checked against a 65,000-byte limit, so replacing the redirect with a rendered
page cannot bypass the budget. Its destination
`/ember/firecracker` measures 69,345 gzip bytes and has an 85,000-byte
initial limit. `/app/frank` serves one
static HTML document with inline scripts, measured at 10,080 gzip bytes and
limited to 12,000 bytes for the whole document.

## Notes budget reduction

The Notes initial limit is 75,000 gzip bytes, reduced from 105,000 after the
first-view and lease fixes landed. Both build variants pass. The same checker
rejects the original 91,207-byte initial closure under the new limit.

| Build | Initial gzip | Deferred gzip | Worker gzip |
| --- | ---: | ---: | ---: |
| Public integration | 64,211 | 29,199 | 0 |
| Full frontend | 65,949 | 29,849 | 0 |
| Limits | 75,000 | 50,000 | 0 |

The public artifacts were built from integration commit
`3a1cd8f5b5364325beb9cb0f0e1b805206e6498e`. Its Notes sources are identical to
landed commit `afc613a4b74ac2c677197b95b4ea4413698806f4`, including the device-sleep
guard. The full build uses `main` at
`a3d1ade2f322fa5550ed427100217b59896a533d` with only the lower budget applied.
The public Notes closure was rechecked from the emitted manifests and files;
the full build ran the adapter gate for all routes. These are gzip level 9
artifact sizes, excluding browser wire overhead. This budget change adds no
runtime speedup.

## Run and inspect

Run the public build gate with the repository's supported CI command:

```sh
ci test -- //projects/monolith/frontend:build_test_public
```

For a local production build after installing the pinned workspace packages:

```sh
python projects/monolith/knowledge/tools/gen_docs_manifest.py
corepack pnpm --dir projects/monolith/frontend build
```

The local build includes the private routes, so its router and shared chunks
can differ slightly from the public artifact. Both builds apply the limits.
The output directory contains `bundle-budgets.json` with each route's raw
and gzip byte counts, file lists, deferred chunks and workers. The default
output is `dist`; the public CI build uses `build_public`.

Lower a limit when a measured reduction lands. Increasing one requires the
before/after report and a reason in the PR. Run browser measurements after
deployment before claiming a user-visible speedup.
