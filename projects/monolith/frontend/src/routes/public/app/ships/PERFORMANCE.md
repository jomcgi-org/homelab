# Ships performance

`/app/ships` loads vessel positions on the server. Track and traffic-density
layers use the same-origin `/track/[mmsi]` and `/heat` proxies.

The landed [server-rendered snapshot change](https://github.com/jomcgi-org/homelab/pull/7972)
shows real data rows while the interactive map loads. Its rollout and mobile
first-view results must be checked separately from the cache and bundle limits.

The pending [vessel animation change](https://github.com/jomcgi-org/homelab/pull/7991)
submits geometry changes only for positions that can still move, at the
existing four-updates-per-second cadence. It preserves the full fleet and
snapshot freshness policy.

## Cache windows

| Responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | ---: | ---: |
| HTML and navigation snapshot | 120 | 600 | 720 (12m) | 86,400 | 86,520 (24h2m) |
| Vessel track | 60 | 300 | 360 (6m) | 86,400 | 86,460 (24h1m) |
| Traffic heat | 300 | 3,600 | 3,900 (65m) | 86,400 | 86,700 (24h5m) |

All three policies set browser `max-age=0`. The loader and proxies mirror the
constants in the [backend router](../../../../../../ships/router.py).

[AIS ingestion](../../../../../../ships/ingest.py) consumes a
continuous feed and flushes at 200 rows or when an incoming message finds the
batch older than two seconds. This is not a guaranteed reporting cadence for
each vessel. The snapshot query has no timestamp cutoff on `latest_positions`;
a vessel without new accepted fixes can remain older than every cache window.
The browser's two-minute refresh and five-minute dead-reckoning clamp do not
bound that source age.

The heat rollup runs hourly and combines recent traffic with banked historical
cells. Heat is a density history, not a live-position layer. The acceptable
source age and outage behavior remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/ships` | 80,000 | 330,000 | 170,000 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
