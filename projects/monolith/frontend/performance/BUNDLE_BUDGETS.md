# Public app bundle budgets

Both frontend production builds check emitted JavaScript before succeeding.
The existing `//projects/monolith/frontend:build_test` and
`//projects/monolith/frontend:build_test_public` CI targets enforce
`bundle-budgets.json`. A new page under `/public/app` or `/public/scotland`
needs an explicit budget. Missing pages, missing chunks and unrecognized
SvelteKit manifest declarations fail the check.

Initial bytes include the SvelteKit runtime, router, layouts, page and their
static imports, counted once per route. Deferred bytes include the selected
route's dynamic imports and their dependencies. The router's imports of other
pages are excluded. Worker scripts have a separate budget. Every file is
compressed independently with gzip level 9, matching the units in the JSON.
The check also rejects MapLibre or Mermaid in the initial import closure.

These are regression limits based on the 2026-11-14 build. They do not establish
the mobile targets of LCP below 1.5 seconds, CLS below 0.05 or INP below 200 ms.
They exclude SSR HTML, inline data, CSS, fonts, images, map tiles and API
responses. The production measurement harness records those transfers and
browser timings separately. In particular, the stars and ships inline
snapshots are larger than their JavaScript bundles.

## Measured initial JavaScript

The baseline uses `main` at `1f536d94934ccfef73aa3a2129f00be60f8a44ca` with
the public source exclusions from `:src_public`: private routes, friends
routes and `src/lib/private` are absent. Values are bytes. The budget check changes no client behavior. Rebuilt gzip sizes differ by
7 to 16 bytes because generated version strings and chunk names change.

| Route | Baseline gzip | After gzip | Initial gzip limit |
| --- | ---: | ---: | ---: |
| `/app/hikes` | 72,096 | 72,085 | 85,000 |
| `/app/ships` | 68,017 | 68,004 | 80,000 |
| `/app/stars` | 72,636 | 72,623 | 85,000 |
| `/app/trips` | 62,834 | 62,825 | 75,000 |
| `/app/trips/[slug]` | 69,684 | 69,677 | 85,000 |
| `/app/trips/[slug]/day/[day]` | 72,402 | 72,394 | 85,000 |
| `/app/trips/[slug]/timeline` | 69,555 | 69,543 | 85,000 |
| `/app/notes` | 91,207 | 91,198 | 105,000 |
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
measured at 146,653 gzip bytes, limited to 170,000 bytes. Trips uses the
library default and has no separate worker asset in its emitted closure.
The other pages have no deferred JavaScript in this baseline. Notes has a
50,000-byte deferred allowance for separating its interactive graph from the
first view; its initial limit still applies independently.

Grimoire keeps its admission and session behavior. Its bundle budgets do not
require server rendering protected data or permit caching session responses.
`/app/firecracker` is a server redirect; its destination
`/ember/firecracker` measures 69,345 gzip bytes and has an 85,000-byte
initial limit. `/app/frank` serves one
static HTML document with inline scripts, measured at 10,080 gzip bytes and
limited to 12,000 bytes for the whole document.

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
