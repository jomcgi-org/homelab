# Leaderboard performance

The server imports the committed leaderboard JSON. There is no first-view
backend query. New benchmark results require regenerating the snapshot and
deploying the committed change.

## Cache windows

| Responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | ---: | ---: |
| HTML and navigation data | 60 | 86,400 | 86,460 (24h1m) | 31,536,000 | 31,536,060 (365d1m) |

The [server loader](+page.server.js) uses
`PAGE_CACHE_CONTROL`. It has no explicit browser `max-age`. The one-minute
shared freshness period does not bound publication visibility while stale
responses are permitted. The benchmark snapshot has its own collection date;
revalidating its HTML does not rerun the benchmark.

Maximum acceptable result age and publication delay remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/llm-leaderboard` | 85,000 | 0 | 0 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
