# Notes performance

The landed [first-view change](https://github.com/jomcgi-org/homelab/pull/7971)
renders counts and at most eight current public note titles. The
complete graph stays in `/app/notes/graph`; its renderer loads progressively.

## Cache windows

| Responses | Freshness bound | SWR | SIE |
| --- | --- | --- | --- |
| HTML and navigation data | `no-store` | None | None |
| Graph and server overview snapshot | At most 60s, capped by earliest review/validity deadline and remaining upstream age | None | None |
| Selected note body | At most 3,600s, capped by that note's review deadline and remaining upstream age | None | None |
| `/stats` rollup | TTL 60s; normal maximum 86,460s (24h1m) | 86,400s | 31,536,000s; error allowance 31,536,060s (365d1m) |

The [snapshot cache](../../../../lib/server/public-snapshots.js)
consumes upstream Date/Age and permits no expired fallback. The
[public backend](../../../../../../knowledge/public_router.py)
filters current public records. The graph query bounds its lease by the
earliest review or validity deadline; the note-body path uses `review_after`
with a 3,600-second policy ceiling and does not separately cap it by
`valid_until`. Empty, missing or exhausted leases are not stored.
The older one-hour graph constant does not override the graph proxy's
60-second snapshot ceiling.

HTML and navigation data are deliberately `no-store` so the overview's
remaining lease can be
matched to the exact request. Browser processing and preload residence consume
that lease; clock discontinuities or expiry remove the titles. The HTML
cache-HIT target remains unresolved in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960).

The stats endpoint is a separate observability rollup, scheduled every five
minutes. Its long stale allowances do not apply to current-only note titles.
Neither a response TTL nor the stats schedule proves that the upstream source
has recently refreshed.

## Build budgets

Gzip bytes, summed per emitted JavaScript file:

| Route | Initial | Deferred | External worker |
| --- | ---: | ---: | ---: |
| `/app/notes` | 75,000 | 50,000 | 0 |

Budget categories and measurement targets are in [the shared contract](../../../../../docs/public-app-cache-windows.md).
