# Stars performance

`/app/stars` receives the forecast site snapshot in its server load. Map-only
and historical layers have separate same-origin endpoints.

The landed [server-rendered snapshot change](https://github.com/jomcgi-org/homelab/pull/7972)
shows real data rows while the interactive map loads. Its rollout and mobile
first-view results must be checked separately from the cache and bundle limits.

## Cache windows

| Responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | ---: | ---: |
| HTML, navigation data, `/map` | 1,800 | 3,600 | 5,400 (90m) | 86,400 | 88,200 (24h30m) |
| `/history`, `/history-map` | 31,536,000 | 604,800 | 32,140,800 (372d) | 604,800 | 32,140,800 (372d) |

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

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/stars` | 85,000 | 330,000 | 170,000 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
