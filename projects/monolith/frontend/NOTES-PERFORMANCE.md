# Notes first view

`/app/notes` renders the public graph's note count, link count and up to eight
connected note titles in its first HTML response. Titles are ordered by degree,
then ID. They link to the existing graph focus view. The full node and edge
arrays stay in `/app/notes/graph`; they are not duplicated in the HTML.

The browser imports the canvas, D3 controls and note renderer after an initial
paint opportunity. The overview occupies the same reserved `80dvh`, minimum
480px, region as the graph. It remains visible until the canvas reports a
successful paint. A failed graph request or unavailable canvas retains current
titles and offers a retry. JavaScript-disabled visitors can read the overview.

## Freshness

The page uses `readPublicSnapshot("graph")`, the same anonymous projection as
the graph proxy. Its maximum shared and browser cache lifetime is 60 seconds,
further limited by the upstream public notes' earliest review or validity
deadline. The snapshot's remaining `Cache-Control`, Cloudflare cache header
and Date are preserved. Kit computes the HTML validator from the page body,
including its absolute deadline, so renewing an unchanged graph cannot
revalidate HTML carrying an expired deadline. There is no stale-while-revalidate or
stale-if-error allowance for current-only note titles.

The overview carries an absolute expiry calculated from the remaining lease,
including Date and Age. Hydration cannot restart it. The browser removes the
overview's titles at that deadline even if the canvas never loads. Missing,
expired, private or no-store snapshot leases produce an unavailable overview
with `no-store` headers. The complete graph retains its existing independent
HTTP lease and refresh behavior.

## Budgets and evidence

The first response contains at most eight titles and no complete graph arrays.
JavaScript budgets are 105,000 gzip bytes for the initial route and shared
imports, 50,000 gzip bytes for deferred imports, and no dedicated worker.
Mobile targets remain LCP below 1.5 seconds, CLS below 0.05 and
interaction latency below 200ms. Production verification must measure them;
component rendering and bundle checks do not establish those results.

On 2026-11-14 the live baseline page was 10,589 decoded bytes and returned
`cf-cache-status: BYPASS` without a cache policy. It contained no note data.
The separately fetched graph was 359,500 decoded bytes with 1,066 nodes and
417 edges. All coordinates were null, so this change preserves the existing
canvas projection rather than inventing a server-rendered graph layout.

Local component measurements use eight synthetic titles from a 1,066-node
overview, one warmup and three renders per version. These exclude SvelteKit's
document, asset links and server-loader time.

| Measurement | Before | After |
| --- | ---: | ---: |
| Readable note titles in server HTML | 0 | 8 |
| Component HTML bytes | 3,294 | 4,956 |
| Component HTML gzip bytes | 1,036 | 1,249 |
| Serialized overview bytes | 0 | 916 |
| Initial JavaScript gzip bytes | 91,207 | 63,607 |
| Initial JavaScript files | 38 | 33 |
| Deferred graph JavaScript gzip bytes | 0 | 29,200 |
| Median component render | 0.364ms | 0.317ms |

JavaScript sizes come from the successful public Vite build and its static
import graph. The overview defers 27,600 gzip bytes from the initial JavaScript
load, a 30.3% reduction. The complete graph remains available after hydration.

The sub-millisecond timing difference is measurement noise. No production
speedup is established by these local measurements.

Adjacent server, SSR and DOM tests run through
`//projects/monolith/frontend:test`. They cover the remaining cache lease,
readable first-response data, expired hydration, graph failures, unavailable
canvas, expiry while the graph fails, and replacement only after canvas paint.
Existing focus, search, filtering, theme, note-panel and parsed-cache tests
continue to exercise the complete graph.
