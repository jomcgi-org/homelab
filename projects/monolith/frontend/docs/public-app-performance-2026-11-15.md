# Public app performance, 15 November 2026

**The complete “instant on mobile” target is not verified.** Real server-rendered data now appears in the primary map apps and Notes, heavy image work is bounded, and cache privacy and bundle budgets have automated checks. The observations below retain slow edge misses, late paint outliers and unavailable measurements. Field INP and a complete Moto G/4G worker profile remain unverified.

Final median LCP is **Hikes 1064 ms, Ships 1036 ms, Stars 1048 ms and Trips index 1380 ms**. Hikes/Ships/Stars first-MISS header arrival is **890/1001/1046 ms** and first-MISS LCP is **1768/1856/1912 ms**. Stars’ naturally ready Historical click remains **304 ms**. These are the last reviewed lab observations, not a claim that every target passed.

PR states are a snapshot at **2026-11-15T05:30:13.419147+00:00**. The structured report in the [evidence archive](#evidence-archive) preserves full SHAs, every intermediate stage, asset cohorts, sample exclusions, observed cache groups and explicit nulls. A rollout check identifies deployed source, while the observed immutable assets identify what cached HTML actually served.

## Target status

| Target | Result |
| --- | --- |
| Repeat HTML HIT and header arrival <200 ms | Nine journeys observe MISS/HIT/HIT; their repeat header arrivals are under 200 ms. Eight remain BYPASS under existing policy; first-MISS core headers are 890–1046 ms. |
| LCP <1.5 s | 13/17 comparable route medians meet the threshold; preserve individual slow samples. |
| CLS <0.05 | 17/17 comparable route medians meet the threshold. |
| INP <200 ms | Unavailable. Scripted Event Timing is reported separately and is not field INP. |
| First-view data without a client waterfall | SSR data rows/titles and route summaries are tested; the DOM milestone is not complete map paint. |
| Cached API p95 <100 ms | 13/13 sampled HIT groups meet the target. |
| Uncached endpoint <300 ms | 6/13 observed MISS groups meet the target; backend cache state remains unknown. |

## Method and scope

The original [HTTP baseline](performance/2026-11-14-baseline.md) predates changes, but an original pre-date browser baseline was unavailable. The earliest canonical browser baseline is the first hosted run after date grouping was deployed and before SSR changes. It must not support a pre-date browser speedup claim. Fresh-connection local curl timings include managed-proxy overhead and remain separate from hosted visitor-like measurements.

Each hosted journey uses three fresh anonymous contexts, Chromium 151.0.7922.34, a 360×800 viewport at DPR 2, 150 ms nominal latency, 200,000 bytes/s down, 93,750 bytes/s up, 4× page-main-thread slowdown and a 15-second observation window after DOMContentLoaded. The physical runners change between AMD EPYC 9V45, AMD EPYC 9V74, AMD EPYC 7763 and Intel Xeon 6975P-C; equal slowdown settings do not make those CPUs equivalent. No browser or CDN cache purge was performed.

The pinned browser’s raw NavigationTiming omits the simulated network delay. Later page-target CDP header arrival is calibrated using three fresh-context control/throttled pairs; raw and calibrated values stay separate. Worker CPU and aggregate page/worker network shaping remain unverified. These are mobile-viewport lab observations, not certification on a Moto G.

Page-cache mode: the earliest baseline uses canonical URLs; the final mode is **keyed**. Stable-keyed measurements repeat one audited query key across three visits and record actual first/repeat edge states. They are distinct from canonical observations; a redirect dropping the key does not establish a fresh destination. Requested URLs, keys and final key retention remain in JSON. Cross-mode differences are descriptive, not isolated causal effects. Endpoint probes keep their independent normal/unique-query modes.

A route comparison requires three successful, single-document samples from the intended served generation. Chunk-recovery reloads are excluded rather than mixing clock origins. Missing, failed, cancelled or inconsistently sized requests make complete transfer or JavaScript totals unavailable; completed-byte lower bounds remain in artifacts. LCP does not measure canvas content. The DOM milestone checks intersection and visibility, not unobscured pixels, image decoding or completed map rendering.

All 17 journeys below are anonymous. Grimoire measures logged-out admission only; protected content is not accessed. Dynamic trip slugs/days are sampled through one fixed public trip. The 26 page families plus Frank are covered by build budgets; retired campsites/dr-jobs remain deliberate 404s. Notes bodies and personalized/session routes are outside the bulk endpoint probe set.

## Route coverage

| Route group | Build-covered families | Browser journeys | Scope |
| --- | ---: | ---: | --- |
| Non-Grimoire public/app Svelte pages | 10 | 10 | Primary apps, Notes, leaderboard, Firecracker redirect and three representative trip views |
| Grimoire Svelte pages | 11 | 1 | Logged-out admission only; ten protected/session/content families are not bulk benchmarked |
| Scotland Svelte pages | 4 | 4 | Landing, privacy, terms and support |
| Ember Firecracker destination | 1 | 1 | Redirect destination measured separately |
| Frank raw document | 1 | 1 | Separate document gzip budget; four broken image dependencies retained |
| Total | 26 Svelte + 1 document | 17 | Dynamic IDs/slugs are representative samples, not every URL |

There are 21 Svelte page families under public/app plus Frank. The explicit endpoint inventory has 16 families: nine have bulk public data probes, while six Grimoire endpoints and the selected Notes body are excluded from bulk probing. Four generated SvelteKit data envelopes bring the measured endpoint set to 13. Retired campsites and dr-jobs remain intentional 404s, not missing successful pages.

## Baseline versus final journey measurements

Table reference: **earliest canonical, post-date/pre-SSR**. The original canonical baseline and paired keyed before are both retained in JSON and are never silently substituted. Numbers are medians. An unavailable cell is not zero. The final generation/eligibility column explains missing medians. Field INP is unavailable for every route.

| Journey | LCP ms, baseline / final | Visible DOM ms, baseline / final | CLS, baseline / final | Final generation / eligibility |
| --- | ---: | ---: | ---: | --- |
| `/app/hikes` | 1,836 / 1,064 | unavailable / 1,061 | 0.00006 / 0.00312 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/ships` | 4,100 / 1,036 | unavailable / 1,032 | 0.00314 / 0.00314 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/stars` | 3,828 / 1,048 | unavailable / 1,047 | 0.00163 / 0.00163 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/trips` | 1,440 / 1,380 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/scotland` | 884 / 868 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/llm-leaderboard` | 848 / 876 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/notes` | 892 / 828 | unavailable / 825 | 0.00010 / 0.00105 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/grimoire` | unavailable / 2,164 | unavailable / unavailable | unavailable / 0.00339 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/frank` | 288 / 288 | unavailable / unavailable | 0.00000 / 0.00000 | standalone document; 3/3 eligible |
| `/app/firecracker` | 1,120 / 1,048 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/ember/firecracker` | 896 / 888 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/scotland/privacy` | 876 / 872 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/scotland/terms` | 876 / 860 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/scotland/support` | 856 / 864 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/trips/2025-liard-hot-springs` | 6,244 / 6,396 | unavailable / 1,114 | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/trips/2025-liard-hot-springs/timeline` | 4,140 / 1,648 | unavailable / unavailable | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |
| `/app/trips/2025-liard-hot-springs/day/1` | 2,016 / 2,036 | unavailable / 1,115 | 0.00000 / 0.00000 | app.w7YGTDvJ.js; 3/3 eligible |

| Journey | Raw nav ms, baseline / final | Calibrated header ms, baseline / final | Lab click ms, baseline / final | Edge baseline → final |
| --- | ---: | ---: | ---: | --- |
| `/app/hikes` | 50 / 34 | unavailable / 184 | unavailable / 24 | EXPIRED×1, HIT×2 → MISS×1, HIT×2 |
| `/app/ships` | 51 / 33 | unavailable / 184 | unavailable / 8 | EXPIRED×1, HIT×2 → MISS×1, HIT×2 |
| `/app/stars` | 66 / 34 | unavailable / 183 | unavailable / 304 | EXPIRED×1, HIT×2 → MISS×1, HIT×2 |
| `/app/trips` | 53 / 31 | unavailable / 184 | unavailable / unavailable | HIT×3 → MISS×1, HIT×2 |
| `/scotland` | 92 / 36 | unavailable / 184 | unavailable / unavailable | BYPASS×3 → BYPASS×3 |
| `/app/llm-leaderboard` | 54 / 32 | unavailable / 183 | unavailable / unavailable | EXPIRED×1, HIT×2 → MISS×1, HIT×2 |
| `/app/notes` | 44 / 28 | unavailable / 183 | unavailable / unavailable | BYPASS×3 → BYPASS×3 |
| `/app/grimoire` | unavailable / 38 | unavailable / 214 | unavailable / unavailable | unavailable → BYPASS×3 |
| `/app/frank` | 53 / 39 | unavailable / 168 | unavailable / unavailable | EXPIRED×1, HIT×2 → MISS×1, HIT×2 |
| `/app/firecracker` | 216 / 188 | unavailable / 341 | unavailable / unavailable | BYPASS×3 → BYPASS×3 |
| `/ember/firecracker` | 49 / 28 | unavailable / 183 | unavailable / unavailable | BYPASS×3 → BYPASS×3 |
| `/scotland/privacy` | 55 / 28 | unavailable / 183 | unavailable / unavailable | BYPASS×3 → BYPASS×3 |
| `/scotland/terms` | 61 / 25 | unavailable / 177 | unavailable / unavailable | BYPASS×3 → BYPASS×3 |
| `/scotland/support` | 168 / 29 | unavailable / 183 | unavailable / unavailable | BYPASS×3 → BYPASS×3 |
| `/app/trips/2025-liard-hot-springs` | 40 / 29 | unavailable / 192 | unavailable / unavailable | REVALIDATED×1, HIT×2 → MISS×1, HIT×2 |
| `/app/trips/2025-liard-hot-springs/timeline` | 53 / 32 | unavailable / 191 | unavailable / unavailable | REVALIDATED×1, HIT×2 → MISS×1, HIT×2 |
| `/app/trips/2025-liard-hot-springs/day/1` | 45 / 44 | unavailable / 184 | unavailable / 104 | REVALIDATED×1, HIT×2 → MISS×1, HIT×2 |

| Journey | Transfer KiB, baseline / final | JS KiB, baseline / final | Requests, baseline / final | Field INP |
| --- | ---: | ---: | ---: | --- |
| `/app/hikes` | unavailable / unavailable | unavailable / unavailable | 83 / 85 | unavailable |
| `/app/ships` | unavailable / unavailable | unavailable / unavailable | 71 / 73 | unavailable |
| `/app/stars` | unavailable / unavailable | unavailable / unavailable | 79 / 82 | unavailable |
| `/app/trips` | 180.7 / 187.4 | 70.0 / 76.5 | 49 / 49 | unavailable |
| `/scotland` | 145.5 / 161.0 | 80.3 / 92.2 | 49 / 51 | unavailable |
| `/app/llm-leaderboard` | 155.8 / 163.4 | 75.9 / 83.7 | 45 / 45 | unavailable |
| `/app/notes` | 225.9 / 247.4 | 97.1 / 116.1 | 55 / 58 | unavailable |
| `/app/grimoire` | unavailable / 318.9 | unavailable / 114.3 | unavailable / 63 | unavailable |
| `/app/frank` | unavailable / unavailable | 10.2 / 10.2 | 8 / 8 | unavailable |
| `/app/firecracker` | unavailable / unavailable | 84.9 / 84.9 | 55 / 55 | unavailable |
| `/ember/firecracker` | 144.5 / 154.0 | 75.4 / 84.9 | 54 / 54 | unavailable |
| `/scotland/privacy` | 146.6 / 160.7 | 80.8 / 93.4 | 50 / 53 | unavailable |
| `/scotland/terms` | 142.1 / 159.0 | 76.9 / 92.3 | 50 / 52 | unavailable |
| `/scotland/support` | 137.9 / 159.8 | 73.0 / 93.3 | 50 / 53 | unavailable |
| `/app/trips/2025-liard-hot-springs` | unavailable / unavailable | unavailable / unavailable | 70 / 86 | unavailable |
| `/app/trips/2025-liard-hot-springs/timeline` | 1,279.5 / 855.1 | 75.4 / 89.3 | 102 / 68 | unavailable |
| `/app/trips/2025-liard-hot-springs/day/1` | unavailable / unavailable | unavailable / unavailable | 206 / 76 | unavailable |

## First visits and worst samples

A median can hide an origin MISS/EXPIRED response. This table retains the first requested sample, observed MISS samples, and worst eligible header/LCP samples from each final accepted generation. An excluded first navigation remains labeled excluded rather than counted as cold-load success. With a stable query key, repeats use the same key; only actual returned edge states are reported.

| Journey | Final page-cache mode / key retention | First requested edge / header / LCP ms | MISS samples | Worst eligible header / LCP ms |
| --- | --- | --- | --- | --- |
| `/app/hikes` | keyed / keyed | MISS / 890 / 1,768 | run 1: 890 / 1,768 | 890 / 1,768 |
| `/app/ships` | keyed / keyed | MISS / 1,001 / 1,856 | run 1: 1,001 / 1,856 | 1,001 / 1,856 |
| `/app/stars` | keyed / keyed | MISS / 1,046 / 1,912 | run 1: 1,046 / 1,912 | 1,046 / 1,912 |
| `/app/trips` | keyed / keyed | MISS / 219 / 1,408 | run 1: 219 / 1,408 | 219 / 1,408 |
| `/scotland` | keyed / keyed | BYPASS / 184 / 872 | none observed | 190 / 872 |
| `/app/llm-leaderboard` | keyed / keyed | MISS / 183 / 872 | run 1: 183 / 872 | 198 / 888 |
| `/app/notes` | keyed / keyed | BYPASS / 183 / 824 | none observed | 184 / 828 |
| `/app/grimoire` | keyed / keyed | BYPASS / 214 / 2,164 | none observed | 229 / 2,188 |
| `/app/frank` | keyed / keyed | MISS / 167 / 292 | run 1: 167 / 292 | 168 / 292 |
| `/app/firecracker` | keyed / key-not-retained | BYPASS / 341 / 1,048 | none observed | 342 / 1,056 |
| `/ember/firecracker` | keyed / keyed | BYPASS / 181 / 884 | none observed | 184 / 892 |
| `/scotland/privacy` | keyed / keyed | BYPASS / 183 / 872 | none observed | 198 / 884 |
| `/scotland/terms` | keyed / keyed | BYPASS / 177 / 860 | none observed | 183 / 868 |
| `/scotland/support` | keyed / keyed | BYPASS / 184 / 864 | none observed | 184 / 868 |
| `/app/trips/2025-liard-hot-springs` | keyed / keyed | MISS / 1,068 / 7,300 | run 1: 1,068 / 7,300 | 1,068 / 7,300 |
| `/app/trips/2025-liard-hot-springs/timeline` | keyed / keyed | MISS / 1,205 / 2,672 | run 1: 1,205 / 2,672 | 1,205 / 2,672 |
| `/app/trips/2025-liard-hot-springs/day/1` | keyed / keyed | MISS / 1,009 / 2,852 | run 1: 1,009 / 2,852 | 1,009 / 2,852 |

## Endpoint distributions

Reference normal series: earliest canonical, post-date/pre-SSR. The paired keyed option uses its contemporaneous normal API series; the page query key itself never changes endpoint URLs. The earliest canonical stage has no 20-sample MISS series, so its reference MISS column remains unavailable.

Values are p50 / p95 full-response milliseconds from 20 sequential, unthrottled public GETs. Normal keys, observed HIT groups and unique-query probes remain separate. Query keys were audited as ignored by the 13 handlers. A query probe only establishes the returned edge status; even MISS does not prove a cold origin cache or database. Nearest-rank p95 over 20 samples excludes the single largest sample, so maxima are also shown.

| Endpoint | Original HTTP p50/p95 | Reference normal p50/p95 | Final normal p50/p95 | Final HIT p50/p95 | Final normal states / maximum ms |
| --- | ---: | ---: | ---: | ---: | --- |
| `/app/hikes/__data.json` | 103.8 / 198.2 | 27.0 / 34.6 | 24.6 / 61.9 | 23.2 / 61.9 | EXPIRED×1, HIT×19 / 1,483.6 |
| `/app/ships/__data.json` | 124.3 / 203.2 | 43.4 / 80.8 | 26.3 / 59.6 | 26.1 / 59.6 | EXPIRED×1, HIT×19 / 2,453.4 |
| `/app/stars/__data.json` | 114.5 / 171.9 | 55.2 / 113.9 | 28.3 / 48.1 | 28.2 / 48.1 | EXPIRED×1, HIT×19 / 2,621.5 |
| `/app/trips/__data.json` | 19.9 / 33.2 | 21.1 / 30.4 | 9.3 / 17.5 | 9.2 / 17.5 | EXPIRED×1, HIT×19 / 159.7 |
| `/app/hikes/walk/7fc20756-89c2-5280-9be9-3ddb3db01e44` | 12.8 / 55.9 | unavailable | 9.2 / 35.5 | 9.1 / 35.5 | EXPIRED×1, HIT×19 / 205.3 |
| `/app/ships/heat` | 82.2 / 163.8 | 45.8 / 219.5 | 22.1 / 44.0 | 22.0 / 44.0 | EXPIRED×1, HIT×19 / 3,473.5 |
| `/app/ships/track/369514002` | 16.8 / 33.3 | unavailable | 9.9 / 37.3 | 9.6 / 37.3 | EXPIRED×1, HIT×19 / 261.3 |
| `/app/stars/map` | 103.9 / 157.7 | 23.2 / 40.0 | 14.4 / 24.2 | 14.3 / 24.2 | EXPIRED×1, HIT×19 / 388.9 |
| `/app/stars/history` | 109.6 / 159.0 | unavailable | 17.7 / 29.0 | 17.7 / 29.0 | HIT×20 / 38.4 |
| `/app/stars/history-map` | 108.5 / 159.6 | 27.9 / 42.2 | 16.9 / 36.3 | 16.9 / 36.3 | HIT×20 / 36.3 |
| `/app/notes/graph` | 89.6 / 142.9 | 25.1 / 32.7 | 13.4 / 28.6 | 13.3 / 28.6 | EXPIRED×1, HIT×19 / 486.6 |
| `/app/notes/stats` | 13.6 / 19.3 | 19.5 / 23.7 | 9.6 / 12.5 | 9.7 / 11.7 | UPDATING×14, HIT×6 / 19.9 |
| `/scotland/marketplace.json` | 14.2 / 17.5 | 19.8 / 38.3 | 9.3 / 11.9 | 9.4 / 15.5 | UPDATING×12, HIT×8 / 15.5 |

| Endpoint | Reference MISS p50/p95 ms | Final probe p50/p95 ms | Probe edge states | Final observed MISS p50/p95 ms | Probe maximum ms | Final decoded normal / probe bytes |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| `/app/hikes/__data.json` | unavailable | 817.2 / 923.9 | MISS×20 | 817.2 / 923.9 | 1,052.7 | 510,441 / 510,441 |
| `/app/ships/__data.json` | unavailable | 821.7 / 968.4 | MISS×20 | 821.7 / 968.4 | 1,180.4 | 3,070,069 / 3,070,110 |
| `/app/stars/__data.json` | unavailable | 1,062.8 / 1,154.9 | MISS×20 | 1,062.8 / 1,154.9 | 1,368.3 | 3,229,266 / 3,229,266 |
| `/app/trips/__data.json` | unavailable | 125.3 / 134.6 | MISS×20 | 125.3 / 134.6 | 136.0 | 485 / 485 |
| `/app/hikes/walk/7fc20756-89c2-5280-9be9-3ddb3db01e44` | unavailable | 124.6 / 140.6 | MISS×20 | 124.6 / 140.6 | 149.7 | 846 / 846 |
| `/app/ships/heat` | unavailable | 1,699.2 / 2,181.3 | MISS×20 | 1,699.2 / 2,181.3 | 2,196.9 | 2,388,615 / 2,388,615 |
| `/app/ships/track/369514002` | unavailable | 128.0 / 141.5 | MISS×20 | 128.0 / 141.5 | 205.1 | 41 / 41 |
| `/app/stars/map` | unavailable | 284.3 / 323.1 | MISS×20 | 284.3 / 323.1 | 323.5 | 1,561,615 / 1,561,615 |
| `/app/stars/history` | unavailable | 1,263.1 / 1,669.6 | MISS×20 | 1,263.1 / 1,669.6 | 1,737.8 | 2,513,273 / 2,513,273 |
| `/app/stars/history-map` | unavailable | 354.2 / 468.6 | MISS×20 | 354.2 / 468.6 | 483.1 | 2,513,259 / 2,513,259 |
| `/app/notes/graph` | unavailable | 133.2 / 143.2 | MISS×20 | 133.2 / 143.2 | 442.2 | 359,500 / 359,500 |
| `/app/notes/stats` | unavailable | 121.6 / 137.3 | MISS×20 | 121.6 / 137.3 | 273.6 | 438 / 438 |
| `/scotland/marketplace.json` | unavailable | 114.7 / 122.0 | MISS×20 | 114.7 / 122.0 | 126.1 | 550 / 550 |

The fixed Ships track returned an empty 41-byte body in earlier series and is not a worst-case populated track. Public endpoint durations combine edge, frontend, backend and network cost. Local PostgreSQL fixture EXPLAIN results are available below; production plans and isolated production database p95 remain unverified. No production database writes or account-level Cloudflare changes were made.

## Per-journey request chains

These chains describe observed main-target request order and source dependencies in the latest recorded observation, not an isolated causal proof. Each route has an explicit chain, including shared policy pages. Full initiator inventories remain in hosted artifacts; worker initiators are incomplete. The representative clean sample and exact LCP selector are retained in JSON.

Svelte pages request shared/design/route CSS plus fonts.googleapis.com CSS; fonts.gstatic.com WOFF2 often completes after text LCP. Do not claim those font files always block first text. Map worker requests are outside complete initiator profiling.

| Journey | Initial chain | Progressive work / limit | Source |
| --- | --- | --- | --- |
| `/app/hikes` | HTML → shared, route and MapLibre CSS → SSR walk rows | Hydration → deferred MapLibre/worker/style/tile requests; walk detail only on selection | [source](../src/routes/public/app/hikes/+page.svelte) |
| `/app/ships` | HTML → shared, route and MapLibre CSS → SSR vessel rows | Hydration → MapLibre/worker/style/tile requests; Heat/track are separate layers | [source](../src/routes/public/app/ships/+page.svelte) |
| `/app/stars` | HTML → shared, route and MapLibre CSS → SSR site rows | Hydration → MapLibre/worker → map/history-map data; natural history transfer finishes much later than rows | [source](../src/routes/public/app/stars/+page.svelte) |
| `/app/trips` | HTML → shared/route CSS + first gallery cover request → cover-image LCP | Trip data is inline; no map dependency on the index | [source](../src/routes/public/app/trips/+page.svelte) |
| `/scotland` | Landing HTML → shared/route CSS → SSR paragraph | Static connector landing; marketplace JSON is a separate endpoint | [source](../src/routes/public/scotland/+page.svelte) |
| `/app/llm-leaderboard` | HTML with committed data → shared/route CSS → SSR paragraph/table | No runtime leaderboard API request; hydration adds controls | [source](../src/routes/public/app/llm-leaderboard/+page.svelte) |
| `/app/notes` | HTML with overview titles → shared/route CSS → title-anchor visibility | Deferred GraphView import → /app/notes/graph → lease renewal; late anchor LCP can occur near renewal before its response | [source](../src/routes/public/app/notes/+page.svelte) |
| `/app/grimoire` | Admission HTML → CSS + page-sm WebP request → image LCP | Anonymous admission only; no protected corpus loading is tested | [source](../src/routes/public/app/grimoire/+page.svelte) |
| `/app/frank` | Raw HTML with inline CSS/JS → arrivedMessage text LCP | Four cdn.jomcgi.dev image requests fail DNS; no complete-image first view | [source](../src/routes/public/app/frank/+server.js) |
| `/app/firecracker` | Redirect response → /ember/firecracker HTML → shared/route CSS → SSR paragraph | One redirect adds a navigation hop; final destination also measured separately | [source](../src/routes/public/app/firecracker/+page.server.js) |
| `/ember/firecracker` | Destination HTML → shared/route CSS → SSR paragraph | No data API dependency before initial content | [source](../src/routes/public/ember/firecracker/+page.svelte) |
| `/scotland/privacy` | Policy HTML → shared/PolicyPage CSS → SSR summary paragraph | Same static policy chain as terms/support; no initial data API | [source](../src/routes/public/scotland/privacy/+page.svelte) |
| `/scotland/terms` | Policy HTML → shared/PolicyPage CSS → SSR service paragraph | Same static policy chain as privacy/support; no initial data API | [source](../src/routes/public/scotland/terms/+page.svelte) |
| `/scotland/support` | Policy HTML → shared/PolicyPage CSS → SSR contact paragraph | Same static policy chain as privacy/terms; rollout-time reload sample is excluded | [source](../src/routes/public/scotland/support/+page.svelte) |
| `/app/trips/2025-liard-hot-springs` | HTML → CSS → SSR date range and SVG route; then hydration → map/style/source metadata → late attribution-shaped LCP | OpenFreeMap style/planet/sprite/tile dependencies continue after early route content; no full map-paint claim | [source](../src/lib/public/components/trips/TripMap.svelte) |
| `/app/trips/2025-liard-hot-springs/timeline` | HTML → CSS + six eager gallery images → third-photo LCP | Intersection observer adds two more observed images; remaining photos stay progressively scheduled | [source](../src/lib/public/components/trips/PhotoGrid.svelte) |
| `/app/trips/2025-liard-hot-springs/day/1` | HTML → CSS + visible display-photo request → photo LCP; SVG route already in HTML | Map source readiness and visible-photo completion gate two neighbour image requests | [source](../src/lib/public/components/trips/DayMap.svelte) |

The original primary-map baseline browser-gated visible content behind hydration despite inline snapshots. The newer SSR rows remove that JavaScript dependency from useful content. Trip image request changes affect the image chain, while late map attribution LCP remains distinct from early route visibility.

## Ranked changes and remaining critical paths

1. **Render useful data before map hydration.** Previously the three primary apps inlined data but browser-gated their visible app. Server-rendered snapshot rows now provide real content while heavy maps load. First-hosted → final median LCP is Hikes 1836→1064 ms, Ships 4100→1036 ms and Stars 3828→1048 ms. Final useful rows appear at median 1061/1032/1047 ms; first-MISS LCP remains 1768/1856/1912 ms. The original chain was HTML → app hydration → map import/worker; early visible data removes map initialization from the useful-content dependency. Runner differences prevent isolated timing attribution.
2. **Bound photo work around the visible view.** Trip day changes 152/153/153 image requests to three and median total requests 206→76. Its LCP is 2016→2036 ms; fewer requests did not establish faster paint. Timeline changes 44 images to eight, median requests 102→68 and complete transfer 1,310,160→875,624 bytes. The same third-photo LCP remains, measured 4140→1648 ms across different CPUs. Compared with the later paired keyed reference, day photo priority changes 2044→2036 ms, too small and uncontrolled to claim a material speedup.
3. **Reduce repeated forecast data and date work.** Stars shares identical forecast windows without changing reconstructed values. An intermediate origin MISS returned 3,225,114 decoded bytes while the stale normal key still returned 8,366,841. Final normal and MISS data envelopes are 3,229,266 bytes on the current corpus, and every final Svelte sample uses one intended generation. Local date/serialization benchmarks below remain separate evidence.
4. **Keep history and heat off the first paint, then reduce their cost.** Final Heat MISS p95 is 2181 ms and full Stars history 1670 ms, both still above 300 ms. Stars ready-click median is 304 ms versus 480 ms in the paired keyed reference, with changed CPUs. Natural history completion still requires another 5.74–6.04 seconds after the load cutoff before that click; the cold interaction is not immediate. The separate profiled click median is 336 ms; GeoJSON dispatch sample weights fall from 348–411 ms to 14–17 ms and recursive serializer samples disappear, supporting the targeted transport change without proving the whole interaction meets 200 ms. Worker CPU and subsequent map paint remain unmeasured.
5. **Preserve correctness and operational evidence.** Fix direct trip-day navigation/readiness, map worker URLs and incremental Ships geometry updates, enforce public-only cache boundaries including outer HTTP responses, retain original content semantics, and fail measurements with lost clocks. Public immutable assets now have pre-deployment archive publication and a read-only fallback. Cross-release HTTP checks below retain their failures; old-HTML browser replay and per-pod mixtures remain untested.

## Outliers and limits that remain visible

Notes records late LCP candidates of **12,956 ms**, **4068 ms**, **5964 ms**, **6028 ms** and **8020 ms** in separate runs despite initial titles around 0.8–0.9 seconds. Earlier inspected anchors grow from area 23,842 to 27,232 shortly after graph renewal starts, before its body arrives. This is a refresh/layout/visibility association, not proof that the response body or lease policy is faulty. Medians do not erase those samples.

The final run has 51 eligible single-document visits, one Svelte generation, no HTTP subresource errors and no JavaScript exceptions. Frank still has four image DNS failures per visit at cdn.jomcgi.dev, and each day view aborts four terrain requests; those bytes stay unknown. Its fast text LCP does not mean Frank is complete. Earlier chunk-404 recovery samples remain excluded. Trip summary LCP 6396 ms is required map attribution, while real date/route content appears at median 1114 ms; it is not complete map readiness.

All 13 final HIT p95s are below 62 ms, but normal-key series include slow EXPIRED maxima: Heat 3473.5 ms, Stars data 2621.5 ms and Ships data 2453.4 ms. Nearest-rank p95 over 20 samples excludes one maximum; a fast HIT distribution is not an uncached-response pass. All 260 unique-query probes return MISS, with seven endpoint p95 failures retained in the table.

The earlier 40 ms Stars clicks happened without proven completed history data and are excluded from ready-state comparisons. Later clicks require natural successful response completion and two animation frames after load metrics freeze; this still does not prove parsing or resulting map completion. Any diagnostic profiler observations are labeled separately because instrumentation changes workload and worker stacks are absent.

## Immutable asset acceptance

Archive stage 1 #8020 is verified live at **0.792.59**, source `a7d0b8cd41cb658f23163dc3aef1656571dcfdad`, checked at 2026-11-15T04:25:35.313Z. Argo is Synced/Healthy and its PreSync publisher Job succeeded. The exact completion log was not captured. This verifies archive publication, not old-asset fallback delivery.

The release-A retry captures 253 assets, including 1 worker, across 759 identity/gzip/Brotli responses, all observed HIT with zero errors. It preserves the first attempt's proxy timeout separately. Because these responses came from edge cache, they do not establish archive fallback. Stage 2 #8025: rollout verified at 0.792.62 and current 0.792.63; subsequent HTTP/source checks recorded below, with browser/per-pod limitations. No final browser metric is inferred from the capture or rollout.

The later 0.792.62→0.792.63 comparison covers 197 common paths (591 representations) and 56 removed paths. The original removed-only pass returns 223/224 exact content responses; one cold identity request receives an Envoy 503 and remains a failed observation. All 56 removed paths have non-HIT archive evidence in their other representations. A separately recorded later 200/MISS identity confirmation has exact bytes and the expected strong ETag, giving 253/253 path coverage across observations—not a single clean pass. Its pre-response proxy retry is retained. Earlier bootstrap 174 HTTP 429 responses and the paced continuation remain in the archive.

These are HTTP/source checks. A held old-HTML browser session, per-pod rolling mixtures/alternation and account-wide lifecycle duration were not tested. Current generation content confirms the Stars transport helper and visible-photo high priority; it does not prove every historical generation is recoverable.

## Local validation, budgets and freshness

Combined integration `6038398` passes **1,336 tests across 125 files plus 42 HTTP tests**, full and public builds, all bundle budgets, and 84 public routes with no private/friends routes. The measurement helper has 39 tests; hosted clipping/CSS-transition, redirect/reload and interrupted-inventory fixtures pass. These are correctness and tooling checks, not production speed claims.

Local identical-input date benchmarks reduce Stars night grouping 6641.5→46.1 ms and trip day grouping 251.6→31.5 ms with matching outputs. Local forecast-sharing benchmarks reduce inline bytes 9,271,811→3,093,449, navigation bytes 7,881,498→3,188,909, transform/serialization 342.5→155.8 ms and compile/evaluation 480.5→115.2 ms. Input shape and Node runtime differ from live browser work; no live speedup is inferred from these CPU results.

Build budgets account separately for initial, deferred and worker gzip closures across 26 page families plus Frank. Core initial limits are Hikes/Stars 85,000 bytes, Ships 80,000, Trips index 75,000, trip views 85,000, Notes 75,000, leaderboard 85,000 and Scotland 80,000. Deferred maps have a 330,000-byte ceiling and workers 170,000. These are build limits, not whole-page transfer totals. See [configuration](../performance/bundle-budgets.json), [cache/budget procedure](public-app-cache-windows.md), [measurement instructions](public-app-performance.md) and [hosted workflow](public-app-performance-ci.md).

Only public non-personalized responses may be cached. Session/cookie-dependent/private/friends responses remain no-store. Each app’s PERFORMANCE.md records current TTL+SWR and outage TTL+SIE separately from backend/source age; those are alternative stale windows, not three durations added together. Existing policies were documented rather than guessed. Notes HTML/navigation is no-store, and public graph leases are bounded by review/validity semantics. Approved maximum source age remains with Joe in #7960.

## Local PostgreSQL query evidence

An isolated PostgreSQL 16.14 fixture database used the actual migrations and indexes. All 11 measured SELECTs ran three times with transaction_read_only=on after fixture loading and ANALYZE. No production database was contacted. Raw SQL, plans, buffers and timings are preserved as `evidence/public-app-postgres-explain.json` in the [evidence archive](#evidence-archive), without corpus rows.

| Query | Plan | Rows | Median execution ms |
| --- | --- | ---: | ---: |
| `ships_heat_live_before` | Seq Scan | 10,000 | 0.793 |
| `ships_heat_live_scalars` | Seq Scan | 10,000 | 1.027 |
| `ships_heat_history_before` | Seq Scan | 145,020 | 11.484 |
| `ships_heat_history_scalars` | Seq Scan | 145,020 | 15.011 |
| `ships_snapshot_before` | Hash Join | 7,786 | 3.417 |
| `ships_snapshot_scalars` | Hash Join | 7,786 | 3.624 |
| `hikes_walks_current` | Sort | 1,653 | 0.701 |
| `hikes_walks_scalar_prototype` | Sort | 1,653 | 0.611 |
| `hikes_upcoming_hours` | Seq Scan | 44,631 | 6.355 |
| `stars_history_metadata` | Seq Scan | 14,234 | 1.596 |
| `stars_history_months` | Seq Scan | 170,808 | 19.699 |

The Heat split, Hikes hour occupancy and zero climatology months are synthetic. These freshly loaded/analyzed local tables have warm buffers and do not reproduce production statistics, bloat, metadata widths, topology, concurrency, driver transfer, Python hydration, JSON encoding or SvelteKit. The Hikes scalar query is an unlanded prototype.

Whole-table scans are expected for responses returning most/all rows. Scalar Heat/Snapshot SQL is not faster in this fixture; the separately measured benefits concern Python object hydration and response encoding. No new index or production SQL speedup is justified by these plans.

Live screenshot-service verification after rollout 0.792.53 returned HTTP 200 at 390×844 for Hikes and Stars and showed real snapshot rows, through the previously failing path; it is functional evidence, not mobile timing. Live fallback verification after rollout 0.792.47 / c7a3918 observes app.B-J_TtcW.js: the trip summary retains nine bounded SVG routes and all nine daily rows; Day 1 retains its 288-point caption and real photo. Repeat trip HTML is HIT. Ships retains six SSR rows; its later repeat is UPDATING at age 279 seconds within the existing stale allowance. Desktop no-WebGL checks pass next-photo/day navigation and unavailable-Heat recovery to Vessels/filters. Recoverable Retry and summary highlight-photo completion remain unverified. This is correctness evidence, not mobile timing.

## PR state and exact heads

States below distinguish merged code, verified runtime/tooling and pending acceptance. A live rollout alone does not prove a performance improvement. Full merge SHAs are retained in JSON.

| PR | Change | Disposition | Evidence / remaining limit | Head SHA |
| --- | --- | --- | --- | --- |
| [#7963](https://github.com/jomcgi-org/homelab/pull/7963) | Date formatting | merged and verified live | merged and verified live | `4a73fe4fe5237cf0a0e74c6acac16404a557fae4` |
| [#7967](https://github.com/jomcgi-org/homelab/pull/7967) | Session cache boundary | merged and verified live | merged and verified live | `3990f0c87ba60da1a2fedd74a393a09835d7bd1f` |
| [#7969](https://github.com/jomcgi-org/homelab/pull/7969) | Bundle budgets | merged | merged; full/public bundle gates verified | `29a56adc6b2fce98ebd3f9f2251adb13a8e83d44` |
| [#7971](https://github.com/jomcgi-org/homelab/pull/7971) | Notes SSR overview | merged and verified live | merged and verified live | `c1a93441ed295c8987f7aa50713fa0fdc9b6654c` |
| [#7972](https://github.com/jomcgi-org/homelab/pull/7972) | Hikes Ships Stars SSR snapshots | merged and verified live | merged and verified live | `396a3cbd17cbdd907984d22d00ee5a0b5151abad` |
| [#7974](https://github.com/jomcgi-org/homelab/pull/7974) | Trip photos and navigation | merged and verified live | merged and verified live | `9aa444b14f3d4697dc4423a0099411a781c8833b` |
| [#7975](https://github.com/jomcgi-org/homelab/pull/7975) | Measurement harness | merged | merged; hosted workflow verified | `a2242f86147d3d22cf691a5136ce297bc3048967` |
| [#7987](https://github.com/jomcgi-org/homelab/pull/7987) | Trip worker asset | merged and verified live | merged and verified live | `f0600d8929b73578628b9ec10a1b0015b04478b2` |
| [#7989](https://github.com/jomcgi-org/homelab/pull/7989) | Trip route previews | merged and verified live | merged and verified live at 0.792.47; HTTP and no-WebGL browser acceptance passed | `5d2b03627904e3f0c56e836701e684562e3e9d5a` |
| [#7991](https://github.com/jomcgi-org/homelab/pull/7991) | Ships incremental animation | merged and verified live | merged and verified live; production speedup not isolated | `aaae33d1c329e3b490e0746b69c1c5fdb6c577e4` |
| [#7992](https://github.com/jomcgi-org/homelab/pull/7992) | Calibrated measurement evidence | merged | merged; hosted calibration verified | `7cfbb0dc7ddb42ad88d17f9a22780a10c19d799c` |
| [#7993](https://github.com/jomcgi-org/homelab/pull/7993) | App budgets and freshness docs | merged | merged; documentation verified | `08ee196cb4d1539d8b9a777ae017c8b8e442b2f2` |
| [#7994](https://github.com/jomcgi-org/homelab/pull/7994) | Leaderboard fixture readiness | merged | merged; hosted fixture validation passed | `ddca5138d9d8a05a754365c421315eac62852434` |
| [#7996](https://github.com/jomcgi-org/homelab/pull/7996) | Notes budget ratchet | merged | merged; Notes budget gate verified | `e4e6f7855e57238cc116eadeffe04907c93bcccc` |
| [#7997](https://github.com/jomcgi-org/homelab/pull/7997) | Stars Historical interaction | merged and verified live | merged and verified live at 0.792.63; isolated speedup not established | `bad858ad03b87f5f466d8a3e41d50c7f29c614b1` |
| [#7999](https://github.com/jomcgi-org/homelab/pull/7999) | Timeline visible photos | merged and verified live | merged and verified live at 0.792.43; canonical public payload acceptance passed | `4103300d88002200ea1ca3bc5f2d4257f3ff5b9b` |
| [#8000](https://github.com/jomcgi-org/homelab/pull/8000) | Stars forecast sharing | merged and verified live | merged and verified live at 0.792.43; canonical public payload acceptance passed | `fa8cc3ababdf075b748a274f5fd26d3025db61af` |
| [#8002](https://github.com/jomcgi-org/homelab/pull/8002) | Navigation and endpoint probes | merged | merged; hosted navigation fixtures and probes verified | `cc871b91d4d9a2a046ba861337b37a671e303c21` |
| [#8004](https://github.com/jomcgi-org/homelab/pull/8004) | Screenshot public host routing | merged and verified live | merged and verified live at 0.792.53; Hikes/Stars screenshot HTTP200 and real rows confirmed | `2ce0c69300ad27d14cbacc1c75f59a6085e8fbed` |
| [#8005](https://github.com/jomcgi-org/homelab/pull/8005) | Ships unavailable Heat control | merged and verified live | merged and verified live at 0.792.47; HTTP and no-WebGL browser acceptance passed | `4b32a96b5f3b5462dc912ceb30948718df3e44e0` |
| [#8006](https://github.com/jomcgi-org/homelab/pull/8006) | Ships heat scalar reads and direct encoding | merged and verified live | merged and verified live | `c6a95edf0a98baeec86ea6f9c299fa270ab4b1d3` |
| [#8008](https://github.com/jomcgi-org/homelab/pull/8008) | Stars click CPU diagnostic | merged | merged; three-run hosted CPU diagnosis verified | `9c5b8475e2cb75b9482656bc85c9e9f9f0400098` |
| [#8018](https://github.com/jomcgi-org/homelab/pull/8018) | Stars full-history direct HTTP encoding | merged and verified live | merged and verified live | `9fedd176566c5f4ced4747d3f9c73ff8dedac56c` |
| [#8019](https://github.com/jomcgi-org/homelab/pull/8019) | Ships snapshot scalar reads and direct encoding | merged and verified live | merged and verified live | `ef01df037bf9fc975423972d42eedfa46129bb87` |
| [#8020](https://github.com/jomcgi-org/homelab/pull/8020) | Archive public immutable assets before deployment (stage 1) | merged and verified live | merged and verified live at 0.792.59; archive publication only | `182a4c80d227bdf899517b866e32627d60aa3f10` |
| [#8021](https://github.com/jomcgi-org/homelab/pull/8021) | Keep active happy-dom observer callbacks through garbage collection | merged | merged; required CI verified test-only dependency fix | `9ae33a11f5c7daa81d3e875ff5f529d8911a9bcc` |
| [#8022](https://github.com/jomcgi-org/homelab/pull/8022) | Identify fresh page cache measurement cohorts | merged | merged; keyed hosted fixtures and production observations verified | `6bd5b82c79761e829a0b599c11d68324a800028b` |
| [#8024](https://github.com/jomcgi-org/homelab/pull/8024) | Stars historical worker URL transport | merged and verified live | merged and verified live at 0.792.63; ready click 304 ms remains above 200 ms | `1ca735e778acf71f3c8c832de179ee95a6e29afd` |
| [#8025](https://github.com/jomcgi-org/homelab/pull/8025) | Serve retained public assets and verify startup (stage 2) | merged and verified live | merged and verified live at 0.792.63; composite later 253/253 path coverage | `eadc0b4b0567712aa038570d4eea68e6f254bbb5` |
| [#8026](https://github.com/jomcgi-org/homelab/pull/8026) | Prioritize visible Trips day photo | merged and verified live | merged and verified live at 0.792.63; no meaningful day LCP speedup established | `764039a784cad182c0d49e2670fd23ae86b8b7f2` |

## Owner issues and unavailable evidence

| Issue | Outstanding decision or work |
| --- | --- |
| [#7960](https://github.com/jomcgi-org/homelab/issues/7960) | Approve maximum source freshness and existing stale/outage windows per app, including static Scotland/policy/Ember/redirect HTML with no existing maximum. Preserve their BYPASS until that decision; source cadence alone is not a freshness guarantee. |
| [#7961](https://github.com/jomcgi-org/homelab/issues/7961) | Confirm account-level edge identity/session bypass before origin; repo header tests cannot establish every account rule. |
| [#7988](https://github.com/jomcgi-org/homelab/issues/7988) | Restore the four original approved Frank assets/domain; original images are absent from the repo. |
| [#7998](https://github.com/jomcgi-org/homelab/issues/7998) | Archive/fallback are deployed and later HTTP coverage is recorded, preserving the original 503. Remaining acceptance covers unavailable historic generations, held old-HTML browser sessions, per-pod mixtures and lifecycle duration. |
| [#7962](https://github.com/jomcgi-org/homelab/issues/7962) | Track first-MISS origin cost, seven slow endpoint MISS series, ready Stars interaction, remaining Trips/Grimoire LCP, Notes late paints, physical-device/worker/field-INP evidence and representative dynamic-route coverage. |

No production database plan, physical Moto G run, field INP collection, complete worker-throttle validation or exhaustive dynamic-route measurement was available. Local fixture EXPLAIN is recorded separately. Those gaps remain unverified rather than passes.

## Evidence archive

The complete `public-app-performance-evidence.zip` accompanies delivery. The [evidence manifest](performance/2026-11-15-evidence-manifest.json) names its exact report/plan members, source hashes and public workflow artifacts. The archive preserves full structured metrics, request inventories, calibration, sample exclusions, local plans and separate CPU profiles; it is intentionally outside this documentation PR. Its checksum is delivered beside the archive, avoiding a circular archive/report hash. A private account link is not embedded in this public repository.

## Compact measurement provenance

All intermediate metrics remain in JSON rather than repeating historical tables. Hosted artifacts include full request inventories and live timing evidence; the compact report stores no public corpus text. Artifact retention is 90 days.

| Stage | Run / artifact | UTC window | Runner CPU | Calibration |
| --- | --- | --- | --- | --- |
| hostedBaseline | [43652846917](https://github.com/jomcgi-org/homelab/actions/runs/43652846917) | 2026-11-14T23:22:09.810Z to 2026-11-14T23:36:15.512Z | AMD EPYC 9V45 96-Core Processor | unavailable |
| intermediate | [43656057036](https://github.com/jomcgi-org/homelab/actions/runs/43656057036) | 2026-11-15T00:01:52.953Z to 2026-11-15T00:16:23.689Z | Intel(R) Xeon(R) 6975P-C | unavailable |
| calibratedCandidate | [43657734828](https://github.com/jomcgi-org/homelab/actions/runs/43657734828) | 2026-11-15T00:22:00.966Z to 2026-11-15T00:37:04.592Z | Intel(R) Xeon(R) 6975P-C | page target passed |
| sizedCandidate | [43657997410](https://github.com/jomcgi-org/homelab/actions/runs/43657997410) | 2026-11-15T00:37:48.367Z to 2026-11-15T00:53:07.014Z | AMD EPYC 7763 64-Core Processor | page target passed |
| probeCandidate | [43661689657](https://github.com/jomcgi-org/homelab/actions/runs/43661689657) / [artifact](https://github.com/jomcgi-org/homelab/actions/runs/43661689657/artifacts/14140888653) | 2026-11-15T01:09:43.261Z to 2026-11-15T01:30:29.686Z | AMD EPYC 7763 64-Core Processor | page target passed |
| midCandidate | [43662807790](https://github.com/jomcgi-org/homelab/actions/runs/43662807790) / [artifact](https://github.com/jomcgi-org/homelab/actions/runs/43662807790/artifacts/14141372281) | 2026-11-15T01:31:24.808Z to 2026-11-15T01:50:43.131Z | Intel(R) Xeon(R) 6975P-C | page target passed |
| diagnosticCandidate | [43665083851](https://github.com/jomcgi-org/homelab/actions/runs/43665083851) / [artifact](https://github.com/jomcgi-org/homelab/actions/runs/43665083851/artifacts/14142555995) | 2026-11-15T01:53:02.878Z to 2026-11-15T02:12:44.854Z | Intel(R) Xeon(R) 6975P-C | page target passed |
| backendCandidate | [43665083851](https://github.com/jomcgi-org/homelab/actions/runs/43665083851/attempts/2) / [artifact](https://github.com/jomcgi-org/homelab/actions/runs/43665083851/artifacts/14143920199) | 2026-11-15T02:26:11.230Z to 2026-11-15T02:44:13.530Z | AMD EPYC 9V45 96-Core Processor | page target passed |
| keyedBefore | [43671642559](https://github.com/jomcgi-org/homelab/actions/runs/43671642559) / [artifact](https://github.com/jomcgi-org/homelab/actions/runs/43671642559/artifacts/14145167706) | 2026-11-15T03:17:41.451Z to 2026-11-15T03:35:53.404Z | AMD EPYC 9V74 80-Core Processor | page target passed |
| Final, attempt 2 | [43671642559](https://github.com/jomcgi-org/homelab/actions/runs/43671642559/attempts/2) / [artifact](https://github.com/jomcgi-org/homelab/actions/runs/43671642559/artifacts/14147409580) | 2026-11-15T05:05:05.329Z to 2026-11-15T05:22:50.037Z | Intel(R) Xeon(R) 6975P-C | True |
