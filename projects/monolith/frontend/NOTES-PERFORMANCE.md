# Notes first view

`/app/notes` renders the public graph's note count, link count and up to eight
connected note titles in its first HTML response. Titles are ordered by degree,
then ID. They link to the existing graph focus view. The full node and edge
arrays stay in `/app/notes/graph`; they are not duplicated in the HTML.

The browser imports the canvas, D3 controls and note renderer after an initial
paint opportunity. The overview occupies the same reserved `80dvh`, minimum
480px, region as the graph. It remains visible until the canvas reports a
successful paint. A failed graph request or unavailable canvas retains current
titles and offers a retry. A failed module download instead retries with a fresh
document, because browsers can retain module failures for the document's life.
JavaScript-disabled visitors can read the overview.

## Freshness

The page uses `readPublicSnapshot("graph")`, the same anonymous projection as
the graph proxy. The snapshot's maximum cache lifetime is 60 seconds, further
limited by the upstream public notes' earliest review or validity deadline.
There is no stale-while-revalidate or stale-if-error allowance for current-only
note titles. The complete graph retains its existing independent HTTP lease,
cache headers and refresh behavior.

HTML and SvelteKit data responses are deliberately `no-store`. A cached document
cannot reliably expose its current CDN age to page JavaScript, and the browser
wall clock cannot establish a review deadline. The HTML cache-HIT target remains
outstanding for Notes; [#7960](https://github.com/jomcgi-org/homelab/issues/7960)
tracks the cache and freshness decisions.

The server consumes upstream Date, Age and processing time before serializing
the remaining lease. Each response receives a unique `Server-Timing` token.
The browser matches that token to its navigation or resource timing entry and
starts the remaining lease at that request's monotonic start time. This
conservatively subtracts origin work, transfer, hydration and any time spent in
SvelteKit's preload cache. Missing timing uses document start, never a new lease.
The absolute expiry is used only by server rendering. The browser also uses
the document's original clock pair: `performance.timeOrigin` and monotonic zero.
That pair predates the response, hydration and any data prefetch. Only elapsed
client wall time is used, so a constant clock offset is harmless. Either elapsed
clock can end the lease; neither can extend it. Clock reversal or divergence
over 100ms fails closed, with that tolerance allowing timer precision rounding.
A requestAnimationFrame check synchronously removes expired titles before
paint, including after device sleep that paused monotonic time and timers
without a visibility event. Sleep before hydration is detected against the
same original pair. Missing clock-origin support fails closed; a document with
a clock discontinuity needs a fresh navigation to display another overview.
Focus and pageshow also revalidate. The expiry
timer removes titles unconditionally. Page hide or tab backgrounding clears
them before suspension or a back-forward cache restore. Missing, expired,
private or no-store snapshot leases produce an unavailable overview.

## Budgets and evidence

The first response contains at most eight titles and no complete graph arrays.
JavaScript budgets are 75,000 gzip bytes for the initial route and shared
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
load, a 30.3% reduction. These measurements precede the monotonic lease fix.
The complete graph remains available after hydration.

The lease fix was measured separately with the same eight synthetic titles
before and after, one warmup and three renders. Component HTML remained
4,812 bytes, 1,235 gzip bytes, with eight titles. Serialized page data grew
from 772 to 864 bytes for the lease and timing token. Median component rendering
was 0.435ms before and 0.276ms after. With the lease and device-sleep fixes,
the integrated public build measures 64,211 initial and 29,199 deferred gzip
bytes. The full frontend build measures 65,949 initial and 29,849 deferred
gzip bytes. Both pass the reduced initial limit; see the
[bundle budget evidence](performance/BUNDLE_BUDGETS.md#notes-budget-reduction).

The device-sleep guard adds no server data fields or first-view markup. Its
additional 3,600 clock checks took 2.281ms at the median of three local Node
runs (2.635ms, 1.158ms, 2.281ms), after one warmup. This measures the pure guard,
excluding browser frame scheduling and mobile throttling; it is not a browser
performance result. Checks stop when the lease expires or the page unmounts.

The sub-millisecond timing difference is measurement noise. No production
speedup is established by these local measurements.

Adjacent server, SSR and DOM tests run through
`//projects/monolith/frontend:test`. They cover the remaining cache lease,
readable first-response data, expired hydration, graph failures, unavailable
canvas, expiry while the graph fails, and replacement only after canvas paint.
Regressions cover prefetched responses, missing timing, browser clock skew,
backward clock changes, device sleep with paused monotonic time and no lifecycle
events, page suspension, and failed module retry. The sleep guard changes only
browser behavior; its clock pair is not included in server-rendered page data.
Existing focus, search, filtering, theme, note-panel and parsed-cache tests
continue to exercise the complete graph.
