# Grimoire performance

The public layout retains `ssr=false` and its Turnstile admission flow.
Performance work must preserve admission and license gates; protected corpus
content must not be added to anonymous server HTML. Chat and session responses
must remain `no-store`, including errors and navigation data.

## Cache windows

| Cookie-free public read responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/api/[...path]` and `/book/[book]/read` | 3,600 | 86,400 | 90,000 (25h) | 86,400 | 90,000 (25h) |

These proxies use `GRIMOIRE_READ_CACHE_CONTROL` and browser `max-age=0`.
The allowance applies only to public, non-personalized read responses.
Any request carrying a cookie or identity header, response setting a cookie,
or chat/session response is `no-store` under the pending
[response-boundary change](https://github.com/jomcgi-org/homelab/pull/7967).
Page loaders without a cache opt-in also remain `no-store`.

The corpus changes through ingestion and extraction. A successful cache
revalidation does not establish a new source-data observation. The acceptable
publication and outage limits remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960). The first-paint target
must be met within the admission flow; it does not authorize bypassing it.

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/grimoire` | 105,000 | 0 | 0 |
| `/app/grimoire/adventure/[id]` | 90,000 | 0 | 0 |
| `/app/grimoire/book/[book]` | 95,000 | 0 | 0 |
| `/app/grimoire/book/[book]/c/[chunk]` | 90,000 | 0 | 0 |
| `/app/grimoire/chat` | 100,000 | 0 | 0 |
| `/app/grimoire/chat/s/[id]` | 80,000 | 0 | 0 |
| `/app/grimoire/entities` | 90,000 | 0 | 0 |
| `/app/grimoire/entity/[id]` | 95,000 | 0 | 0 |
| `/app/grimoire/explore` | 90,000 | 0 | 0 |
| `/app/grimoire/library` | 90,000 | 0 | 0 |
| `/app/grimoire/world` | 105,000 | 0 | 0 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
