# Hikes performance

`/app/hikes` loads the walk list and forecasts on the server. Walk summaries and
hourly forecast windows arrive through `/app/hikes/walk/[uuid]` when selected.

The landed [server-rendered snapshot change](https://github.com/jomcgi-org/homelab/pull/7972)
shows real data rows while the interactive map loads. Its rollout and mobile
first-view results must be checked separately from the cache and bundle limits.

## Cache windows

| Responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | ---: | ---: |
| HTML, navigation data, walk detail | 1,800 | 3,600 | 5,400 (90m) | 86,400 | 88,200 (24h30m) |

Browsers revalidate (`max-age=0`). The page loader, detail proxy and
[backend router](../../../../../../hikes/router.py) share
`HIKES_WALKS_CACHE_CONTROL`.

The [forecast job](../../../../../../hikes/jobs.py) runs at minute
20 every two hours, per [CronWorkflow values](../../../../../../chart/values.yaml).
Hourly pruning removes elapsed windows; the corpus scrape is manual. Failed
walk fetches retain previous forecast rows. The router excludes elapsed hours,
but does not impose a maximum forecast-source age. `generated_at` is the newest
remaining fetch, not the oldest walk's age. Older comments describing a six-hour
refresh do not match the configured two-hour schedule.

The cache windows therefore do not establish a maximum weather-data age.
That decision remains in [#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/hikes` | 85,000 | 330,000 | 170,000 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
