# Trips performance

The index loads trip summaries. The shared `[slug]` server layout loads trip
metadata and its full ordered point list for summary, timeline and day pages.
Optional photo warming in the day viewer waits for the visible photo and
map readiness; see
[the landed photo work](../../../../../PHOTO-PERFORMANCE.md).
The merged [worker fix](https://github.com/jomcgi-org/homelab/pull/7987)
uses the emitted MapLibre worker instead of a missing relative module URL.
The merged [route-preview change](https://github.com/jomcgi-org/homelab/pull/7989)
renders a bounded SVG route before the interactive map is ready.
Hosted browser measurements and the remaining LCP gaps are in the
[measurement report](../../../../../docs/public-app-performance-2026-11-15.md).

## Cache windows

| Responses | TTL | SWR | Normal maximum | SIE | Error allowance |
| --- | ---: | ---: | ---: | --- | --- |
| Index and trip HTML/navigation data | 300 | 3,600 | 3,900 (65m) | None | No additional SIE window |

Browser freshness is 60 seconds. `TRIPS_CACHE_CONTROL` mirrors `_CACHE` in
[read_router.py](../../../../../../trips/read_router.py). A successful
response can serve a previously cached representation for up to 65 minutes
while revalidation occurs; the five-minute fresh TTL alone is not an edit
propagation bound.

Source content is historical and can be edited or backfilled in place. The
read API reads the stored trip and points without a scheduled refresh or
source-age cutoff. The age of a photograph is not the age of the response.
Acceptable edit visibility and outage behavior remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/trips` | 75,000 | 0 | 0 |
| `/app/trips/[slug]` | 85,000 | 330,000 | 170,000 |
| `/app/trips/[slug]/timeline` | 85,000 | 330,000 | 170,000 |
| `/app/trips/[slug]/day/[day]` | 85,000 | 330,000 | 170,000 |

The worker fix in [#7987](https://github.com/jomcgi-org/homelab/pull/7987)
sets the external-worker limit to **170,000 gzip bytes** for the summary,
timeline and day routes, as recorded above. Initial and deferred budgets stay
unchanged. The trip index remains at zero.
A zero pre-fix limit did not validate the missing worker URL.

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
