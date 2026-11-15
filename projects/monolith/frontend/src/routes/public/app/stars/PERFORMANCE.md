# Stars performance

`/app/stars` receives the forecast site snapshot in its server load. Map-only
and historical layers have separate same-origin endpoints.

The landed [server-rendered snapshot change](https://github.com/jomcgi-org/homelab/pull/7972)
shows real data rows while the interactive map loads. Its rollout and mobile
first-view results must be checked separately from the cache and bundle limits.

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

The backend read and initial JSON parse remain unchanged. A verified rollout
and fresh mobile measurements are required to establish a page speedup.

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
