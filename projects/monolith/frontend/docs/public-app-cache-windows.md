# Public app cache windows

The app-specific `PERFORMANCE.md` files record configured response-cache
allowances and build budgets as of 15 November 2026, including the merged
worker and response-boundary fixes. These documentation updates do not change
those policies. The [measurement report](public-app-performance-2026-11-15.md)
records hosted measurements separately; merge status is not deployment verification.
Source-data age is separate:
serving or revalidating an old database row or materialized object can start a
new response lifetime without refreshing its contents.

All cache-table values are seconds. `TTL` is the shared freshness lifetime;
`SWR` is `stale-while-revalidate`; `SIE` is `stale-if-error`. Normal maximum
response age is `TTL + SWR`. The error allowance is `TTL + SIE`, measured from
the same freshness boundary, not added to SWR. A missing SIE directive grants
no additional error window; any configured SWR window still exists. These are
header allowances, not guarantees that Cloudflare will serve every stale copy.

[`cache-headers.js`](../src/lib/cache-headers.js) defines the route policies.
`cloudflareCacheHeaders()` puts the shared TTL in `s-maxage` for ordinary
caches and `max-age` in `Cloudflare-CDN-Cache-Control`. It preserves each stale
directive. Frank has its own `max-age`-only policy. Cookie-bearing,
authenticated, personalized, private and friends responses must remain
`no-store`; see the [public cache contract](../../../../docs/runbooks/public-tier-checklist.md#public-response-cache-contract)
and the merged [response-boundary work](https://github.com/jomcgi-org/homelab/pull/7967).
That change enforces the boundary at the final HTTP response, including early
redirects and errors. It also marks app pages without an explicit cache policy `no-store`.
A prior edge entry cannot be corrected by an origin header until the edge
contacts the origin; [#7961](https://github.com/jomcgi-org/homelab/issues/7961)
tracks the account-level identity bypass decision.

The existing allowances, including long outage windows, are not approved
product freshness maxima. Joe's choices for maximum acceptable data age,
static-page caching and source-age rejection remain in
[#7960](https://github.com/jomcgi-org/homelab/issues/7960). Notes' current-only
review deadlines are already an enforced bound and must not be extended.

## Budgets and measurement

The current per-route tables match [bundle-budgets.json](../performance/bundle-budgets.json).
The [budget guide](../performance/BUNDLE_BUDGETS.md) describes CI enforcement.
Initial JavaScript includes the router, shared layouts, page and static imports.
Deferred JavaScript counts its reachable dynamic imports. Dedicated emitted
workers are separate. Each file is counted once per category and compressed
with gzip level 9. HTML, inline data, CSS, images and map tiles are outside these
JavaScript limits. A zero external-worker budget does not mean a library never
creates a worker internally. Frank instead has a whole-document gzip limit.

For a cold mobile visitor, the targets are cached HTML TTFB below 200 ms,
LCP below 1.5 s, CLS below 0.05 and INP below 200 ms. Public endpoint full-response
p95 targets are below 100 ms from cache and below 300 ms uncached. Source and
local bundle tests cannot establish those production results.

Use the [measurement harness and workflow](https://github.com/jomcgi-org/homelab/pull/7975)
and its [usage guide](public-app-performance.md)
for three mobile runs per route and 20 read-only samples per endpoint. Record
cache state, deployment identity and failed or incomplete runs. A scripted
interaction's Event Timing is a lab sample; it is not field INP. Re-measure
only after a read-only rollout check confirms the intended deployment.

## App contracts

[Hikes](../src/routes/public/app/hikes/PERFORMANCE.md),
[Ships](../src/routes/public/app/ships/PERFORMANCE.md),
[Stars](../src/routes/public/app/stars/PERFORMANCE.md),
[Trips](../src/routes/public/app/trips/PERFORMANCE.md),
[Notes](../src/routes/public/app/notes/PERFORMANCE.md),
[Leaderboard](../src/routes/public/app/llm-leaderboard/PERFORMANCE.md),
[Scotland](../src/routes/public/scotland/PERFORMANCE.md),
[Frank](../src/routes/public/app/frank/PERFORMANCE.md),
[Firecracker](../src/routes/public/app/firecracker/PERFORMANCE.md) and
[Grimoire](../src/routes/public/app/grimoire/PERFORMANCE.md).

Campsites and dr-jobs are retired routes that return 404.
