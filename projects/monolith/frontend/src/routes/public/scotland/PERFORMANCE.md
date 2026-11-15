# Scotland performance

`/scotland` and its privacy, terms and support pages render committed connector
content. This is the Scotland connector landing page; the retired campsites
app is unrelated.

## Cache windows

| Responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/marketplace.json` | 3,600 | 86,400 | 90,000 (25h) | 31,536,000 | 31,539,600 (365d1h) |

The landing and policy pages have no loader cache opt-in. The pending
[response-boundary change](https://github.com/jomcgi-org/homelab/pull/7967)
marks those app responses `no-store`. Their HTML cache-HIT target remains
open. The [marketplace endpoint](marketplace.json/+server.js)
uses `MARKETPLACE_CACHE_CONTROL` and browser `max-age=0`.

Content changes only with its committed source and deployment. Connector
weather-data freshness belongs to the connector's own data service; this
landing page does not fetch a weather snapshot. Static-page caching and
acceptable catalog publication delay remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/scotland` | 80,000 | 0 | 0 |
| `/scotland/privacy` | 80,000 | 0 | 0 |
| `/scotland/terms` | 80,000 | 0 | 0 |
| `/scotland/support` | 80,000 | 0 | 0 |

Budget categories and measurement targets are in [the shared contract](../../../../docs/public-app-cache-windows.md).
