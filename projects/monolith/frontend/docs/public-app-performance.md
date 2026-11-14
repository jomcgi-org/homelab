# Measure public apps

The runner records three cold-browser page loads under a mobile profile and
prints their medians. It saves every run, response cache headers and request
initiators in `results.json`, plus a Markdown table. Optional public data
endpoints get 20 unthrottled GETs with p50 and p95 full-response durations.

## Run a comparison

Use Node 22 or newer and Playwright 1.62.1 with its Chromium build. Installing
the measurement tool in a temporary directory keeps it out of the frontend's
runtime dependencies:

```bash
npm install --prefix /tmp/homelab-perf --no-save --ignore-scripts playwright@1.62.1
/tmp/homelab-perf/node_modules/.bin/playwright install chromium
cd projects/monolith/frontend
node scripts/public-app-performance.mjs \
  --playwright-module /tmp/homelab-perf/node_modules/playwright/index.mjs \
  --output /tmp/public-apps-before
```

A locally installed `playwright` package is used when `--playwright-module` is
omitted. `--executable /path/to/chromium` selects an existing browser. When a
managed environment requires an HTTP proxy, pass its configured URL through
`--proxy "$HTTPS_PROXY"`. Preserve TLS verification and the environment's
network policy. The runner does not change browser permissions or request
sandbox escalation.

The default pages are hikes, ships, stars, trips, Scotland, the leaderboard and
notes. Repeat `--route` for another journey. Dynamic trip routes need a slug
from the public trip index:

```bash
node scripts/public-app-performance.mjs \
  --playwright-module /tmp/homelab-perf/node_modules/playwright/index.mjs \
  --route /app/stars \
  --endpoint /app/stars/map \
  --endpoint /app/stars/history-map \
  --output /tmp/stars-before
```

Use the same browser, location, profile and observation window before and
after a change. After merge, verify the `monolith-public` rollout, then run
against `https://jomcgi.dev` again. A local build measurement is not evidence
that production became faster.

## What the numbers mean

The profile is a 360 by 800 mobile viewport at device scale 2, 150 ms added
network latency, 1.6 Mbps download, 750 Kbps upload and fourfold CPU slowdown.
This is a reproducible lab approximation, not a calibrated physical Moto G.
Each run uses a new browser context with no cookies, browser cache or service
worker. The runner observes Cloudflare's actual cache state; it never purges
or adds cache-busting query parameters. A browser-cold run can be an edge HIT.
Record misses separately when they occur naturally. A forced origin-cold test
needs a controlled dev environment.

TTFB is navigation `responseStart - startTime`, including connection setup.
The default observation ends 15 seconds after DOMContentLoaded. Increase
`--settle-ms` when the app still loads at that point. LCP is the last observed
candidate; CLS uses the largest session window and excludes shifts after
recent input. Context-wide request events include redirects, dedicated-worker
requests and child-frame requests. `request.sizes()` reports encoded HTTP
response body and header sizes. Transfer and JavaScript totals sum those
sizes for requests completed before the cutoff; connection overhead is
excluded. If a request is unfinished, failed or unsized, the full total stays
`null`. The JSON retains completed-response lower bounds and counts of the
incomplete, failed and unsized requests.

Detailed CDP initiator relationships cover the page target. When workers or
child frames appear, `initiatorsBeforeLcp` stays `null`; the scoped
`pageTargetInitiatorsBeforeLcp` and `pageTargetRequests` remain available for
inspection. The page-target CDP transfer estimate includes headers received
and body chunks, with the finished event replacing the estimate. It is not
used as a whole-page transfer total.

CPU and network throttling are applied to the page target. Worker and child
frame throttling is unverified, so those runs show `profileComplete: false`
and the table counts complete-profile runs separately. Their LCP and CLS are
still observed lab values, but cannot certify the requested full mobile
profile. Inspect the request relationships alongside the page to find its
critical path. A late request alone does not establish that it blocked paint.

INP requires real interaction observations over a page visit. It stays
`null` in this synthetic report. To measure a read-only control, add
`--interaction '/app/hikes::selector'` using its current selector. The runner
clicks it after recording the load sample and reports Event Timing as
`interactionLatencyMs`. It never picks an arbitrary button. Missing or
unobservable samples remain `null`; they do not become zero. A requested
selector that fails to click marks the run failed and makes the command exit
nonzero. A successful click with no observable Event Timing sample remains a
successful run with `null` latency. A fast synthetic click does not establish
field INP below 200 ms.

Endpoint samples are full-response durations through Playwright's request
client, without mobile throttling. They are separate from navigation TTFB.
Use the same-origin public proxies or SvelteKit `__data.json` routes. The
public origin intentionally does not expose backend `/api/*`. A navigation
response includes SvelteKit processing and is not a direct backend query
measurement. Inspect backend SQL with `EXPLAIN ANALYZE` only on a local or dev
database. Do not write to production to construct a baseline.

The runner uses GET for endpoint samples and stores no response bodies.
Explicit interaction selectors must describe read-only controls. Public
Grimoire corpus routes require Turnstile admission and are excluded from the
default run. Their current admission boundary must remain intact.

## Targets and baseline

The goal is repeat-view edge HIT and TTFB below 200 ms, LCP below 1.5 seconds,
CLS below 0.05 and field INP below 200 ms. Cached public APIs target p95 below
100 ms; uncached origin APIs target 300 ms. These targets do not replace the
per-route freshness policy or the public cache isolation tests.

The [14 November baseline](performance/2026-11-14-baseline.md) records three
HTTP runs per page and 20 per public data endpoint. The
[JSON](performance/2026-11-14-baseline.json) records every requested metric,
with `null` for browser metrics that could not be measured. Headless Chromium
could not create its process socket in that environment, and automatic
approval policy rejected the escalated launch. The available interactive
browser had no throttling or performance interface. No LCP, CLS, INP or
browser transfer result is inferred from HTTP.

Page curl requests opened fresh TLS connections through the managed proxy.
That added roughly 3 to 5.6 seconds before the response, including on edge
HITs. Those page timings cannot establish direct visitor TTFB. API requests
reused a connection, so their distributions are reported separately. The
initial API sample is retained in the nearest-rank p95 calculation.

Measured work, in order:

1. Stars sends 9.28 MB of decoded HTML while its visible app is browser-gated.
   Hikes sends 466 kB and ships 2.69 MB with the same first-paint problem.
   Render actual snapshot content before hydration and keep heavy map work
   progressive.
2. A representative trip summary sends 2.28 MB of decoded HTML, its timeline
   3.52 MB, and one day 2.27 MB. Inspect shared full-trip data and eager photo
   loading before changing the payload contract.
3. Scotland and notes HTML return BYPASS in all three runs. Add explicit
   anonymous route policies only after checking their freshness and cookie
   dependencies. Notes also waits for its graph fetch after hydration.
4. Large data endpoints have observed TTFB p95 around 107 to 142 ms; small
   endpoints range from 17 to 56 ms. The baseline table also records full
   response duration and the cache-HIT subset. These public proxy measurements do not
   identify backend query time. Origin profiling remains unverified.

The leaderboard and trip index already contain visible data in HTML. Frank
is static HTML. `/app/firecracker` redirects to `/ember/firecracker`.
Scotland's policy pages are static. Retired campsites and dr-jobs URLs remain
404s. These distinctions matter when interpreting the first-paint target.

## First-view dependencies

These chains come from the baseline source and HTML. Browser initiator timing
was unavailable, so their durations remain unmeasured.

| App | First-view dependency chain | Later data |
| --- | --- | --- |
| Hikes | HTML with snapshot, hydration, HikesApp, lazy MapLibre and worker, map style and tiles | Selected walk detail |
| Ships | HTML with snapshot, hydration, ShipsApp, lazy MapLibre and worker, map style and tiles | Heat grid after idle warmup; selected vessel track |
| Stars | HTML with full sites/windows, hydration and night aggregation, StarsApp, lazy MapLibre and worker, map style and tiles | History and history map after mode change |
| Trip index | HTML with trip cards and signed cover image | Navigation to a trip |
| Trip summary/day | HTML with whole-trip points, hydration, lazy MapLibre, map style and tiles | Photo preloads and viewer images |
| Notes | HTML shell, hydration, GraphView, graph JSON, client graph layout | Selected note body |
| Scotland, leaderboard, Frank | Visible HTML; CSS and fonts affect presentation | Read-only local controls |

The primary map apps already inline their initial data; they do not need a
first-view client API fetch. Their browser-gated visible content still waits
for hydration. Notes has a separate graph request before its data appears.
