# Public app performance, 15 November 2026

**The "instant on mobile" target is not met.** Hikes, Ships and Stars now show server-rendered rows before their maps load, and the trip timeline and day view make fewer requests. Four of 17 journeys still miss the 1.5 s LCP target, seven uncached endpoints exceed 300 ms at p95, and field INP is unavailable.

Final median LCP is Hikes 1,064 ms, Ships 1,036 ms, Stars 1,048 ms and Trips index 1,380 ms. The earliest hosted baseline measured 1,836, 4,100, 3,828 and 1,440 ms. The Stars Historical click median is 304 ms.

Every figure below comes from a committed file, mainly the run tables under [`performance/`](performance/), or from a public page linked beside it. [Not verified](#not-verified) names what was measured but can no longer be checked.

## Target status

| Target | Final run |
| --- | --- |
| Repeat HTML HIT, header arrival under 200 ms | Nine journeys return MISS, HIT, HIT with median header arrival of 168 to 192 ms. Eight stay BYPASS under existing policy. |
| LCP under 1.5 s | 13 of 17 medians. Grimoire (2,164 ms), the trip summary (6,396 ms), timeline (1,648 ms) and day view (2,036 ms) miss. |
| CLS under 0.05 | 17 of 17 medians, all 0.003 or lower. |
| INP under 200 ms | Unavailable. Lab click timings are reported separately and are not field INP. |
| First-view data without a client waterfall | Visible-content medians: Hikes 1,061 ms, Ships 1,032 ms, Stars 1,047 ms, Notes 825 ms, trip summary 1,114 ms, day view 1,115 ms. This DOM milestone is not complete map paint. |
| Cached API p95 under 100 ms | 13 of 13 normal-key series, all 62 ms or lower. |
| Uncached API p95 under 300 ms | 6 of 13 unique-query series. Backend cache state is unknown. |

## Method

Two hosted runs of the [measurement workflow](public-app-performance-ci.md) back this report. Both measured `https://jomcgi.dev` with Chromium 151.0.7922.34, using three new anonymous browser contexts per journey. The [profile](public-app-performance.md#what-the-numbers-mean) is a 360 by 800 viewport at device scale 2, 150 ms added latency, 200,000 bytes/s down, 93,750 bytes/s up and fourfold page CPU slowdown, observed for 15 seconds after DOMContentLoaded. No physical phone was measured.

| Run | Started (UTC) | Runner CPU | Harness | Page cache | Committed files |
| --- | --- | --- | --- | --- | --- |
| Hosted baseline, [43652846917](https://github.com/jomcgi-org/homelab/actions/runs/43652846917) | 2026-11-14 23:22 | AMD EPYC 9V45 | `a2242f8` | unkeyed | [table](performance/2026-11-14-hosted-baseline-run/table.md), [provenance](performance/2026-11-14-hosted-baseline-run/provenance.json) |
| Final, [43671642559 attempt 2](https://github.com/jomcgi-org/homelab/actions/runs/43671642559/attempts/2) | 2026-11-15 05:04 | Intel Xeon 6975P-C | `6bd5b82` | keyed, `__public_perf_page=run-43671642559-attempt-2` | [table](performance/2026-11-15-final-run/table.md), [provenance](performance/2026-11-15-final-run/provenance.json), [calibration](performance/2026-11-15-final-run/calibration.json) |

The baseline started before the server-rendered snapshot change ([#7972](https://github.com/jomcgi-org/homelab/pull/7972)) merged. Date grouping ([#7963](https://github.com/jomcgi-org/homelab/pull/7963)) had merged 12 minutes earlier; its deployment at that time is not verified. Every PR under [Changes measured](#changes-measured) merged before the final run started, the last ([#8026](https://github.com/jomcgi-org/homelab/pull/8026)) eight minutes before. Both provenance files record `deployedSha` as null, so the final run may not include the last merges. Runner CPU, harness revision and page-cache mode all differ between the runs; baseline-to-final differences do not isolate any one change.

The final run's calibration passed. Across three control and throttled pairs, median header arrival rose from 6.8 ms to 156.9 ms, an added 150.1 ms. Raw NavigationTiming in the throttled samples reports about 2 ms, so it omits the simulated latency; the final table keeps header arrival and raw navigation TTFB in separate columns. The calibration covers the page target only. Worker throttling is unverified.

The 17 journeys are anonymous. Grimoire measures logged-out admission only. Dynamic trip routes are sampled through one public trip, `2025-liard-hot-springs`. [Build budgets](../performance/bundle-budgets.json) cover 26 Svelte page families and the Frank document.

## Journey results

Medians of three samples. An unavailable cell is not zero. The baseline table has no header-arrival or visible-content column, no lab click values and no Grimoire row.

| Journey | LCP ms, baseline / final | Final visible content ms | Final CLS | Requests, baseline / final | Final header ms | Final edge states |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `/app/hikes` | 1,836 / 1,064 | 1,061 | 0.003 | 83 / 85 | 184 | MISS, HIT, HIT |
| `/app/ships` | 4,100 / 1,036 | 1,032 | 0.003 | 71 / 73 | 184 | MISS, HIT, HIT |
| `/app/stars` | 3,828 / 1,048 | 1,047 | 0.002 | 79 / 82 | 183 | MISS, HIT, HIT |
| `/app/trips` | 1,440 / 1,380 | unavailable | 0.000 | 49 / 49 | 184 | MISS, HIT, HIT |
| `/scotland` | 884 / 868 | unavailable | 0.000 | 49 / 51 | 184 | BYPASS ×3 |
| `/app/llm-leaderboard` | 848 / 876 | unavailable | 0.000 | 45 / 45 | 183 | MISS, HIT, HIT |
| `/app/notes` | 892 / 828 | 825 | 0.001 | 55 / 58 | 183 | BYPASS ×3 |
| `/app/grimoire` | not measured / 2,164 | unavailable | 0.003 | not measured / 63 | 214 | BYPASS ×3 |
| `/app/frank` | 288 / 288 | unavailable | 0.000 | 8 / 8 | 168 | MISS, HIT, HIT |
| `/app/firecracker` | 1,120 / 1,048 | unavailable | 0.000 | 55 / 55 | 341 | BYPASS ×3 |
| `/ember/firecracker` | 896 / 888 | unavailable | 0.000 | 54 / 54 | 183 | BYPASS ×3 |
| `/scotland/privacy` | 876 / 872 | unavailable | 0.000 | 50 / 53 | 183 | BYPASS ×3 |
| `/scotland/terms` | 876 / 860 | unavailable | 0.000 | 50 / 52 | 177 | BYPASS ×3 |
| `/scotland/support` | 856 / 864 | unavailable | 0.000 | 50 / 53 | 183 | BYPASS ×3 |
| `/app/trips/2025-liard-hot-springs` | 6,244 / 6,396 | 1,114 | 0.000 | 70 / 86 | 192 | MISS, HIT, HIT |
| `/app/trips/2025-liard-hot-springs/timeline` | 4,140 / 1,648 | unavailable | 0.000 | 102 / 68 | 191 | MISS, HIT, HIT |
| `/app/trips/2025-liard-hot-springs/day/1` | 2,016 / 2,036 | 1,115 | 0.000 | 206 / 76 | 184 | MISS, HIT, HIT |

Final lab click medians are Hikes 24 ms, Ships 8 ms, Stars Historical 304 ms and the trip day view 104 ms. The trip summary's LCP median is 6,396 ms; its visible-content milestone is 1,114 ms.

Both tables also hold transfer and JavaScript sizes. The baseline harness reports JavaScript size for some incomplete request profiles and the final harness marks those unavailable, so this report does not compare them.

## Endpoint results

Each series is 20 sequential public GETs. Values are p50 / p95 full-response milliseconds. Final normal-key samples are HIT apart from one EXPIRED sample in nine series and UPDATING samples for Notes stats (14) and the Scotland marketplace (12). Every unique-query probe returns MISS. A MISS shows the edge state only; it does not prove a cold origin cache or database.

| Endpoint | Baseline normal | Final normal | Final unique-query |
| --- | ---: | ---: | ---: |
| `/app/hikes/__data.json` | 27 / 35 | 25 / 62 | 817 / 924 |
| `/app/hikes/walk/7fc20756-89c2-5280-9be9-3ddb3db01e44` | not measured | 9 / 35 | 125 / 141 |
| `/app/ships/__data.json` | 43 / 81 | 26 / 60 | 822 / 968 |
| `/app/ships/track/369514002` | not measured | 10 / 37 | 128 / 141 |
| `/app/ships/heat` | 46 / 220 | 22 / 44 | 1,699 / 2,181 |
| `/app/stars/__data.json` | 55 / 114 | 28 / 48 | 1,063 / 1,155 |
| `/app/stars/map` | 23 / 40 | 14 / 24 | 284 / 323 |
| `/app/stars/history` | not measured | 18 / 29 | 1,263 / 1,670 |
| `/app/stars/history-map` | 28 / 42 | 17 / 36 | 354 / 469 |
| `/app/trips/__data.json` | 21 / 30 | 9 / 17 | 125 / 135 |
| `/app/notes/graph` | 25 / 33 | 13 / 29 | 133 / 143 |
| `/app/notes/stats` | 19 / 24 | 10 / 12 | 122 / 137 |
| `/scotland/marketplace.json` | 20 / 38 | 9 / 12 | 115 / 122 |

Public endpoint durations combine edge, frontend, backend and network cost. The Hikes walk and Ships track are single fixed samples.

## What changed

1. **Useful data renders before map hydration.** [#7972](https://github.com/jomcgi-org/homelab/pull/7972) server-renders Hikes, Ships and Stars rows; [#7971](https://github.com/jomcgi-org/homelab/pull/7971) does the same for Notes titles. Median LCP fell from 1,836 to 1,064 ms (Hikes), 4,100 to 1,036 ms (Ships) and 3,828 to 1,048 ms (Stars). Final visible rows appear at 1,061, 1,032 and 1,047 ms.
2. **Photo work is bounded around the visible view** ([#7974](https://github.com/jomcgi-org/homelab/pull/7974), [#7999](https://github.com/jomcgi-org/homelab/pull/7999), [#8026](https://github.com/jomcgi-org/homelab/pull/8026)). Day-view median requests fell from 206 to 76; its LCP did not improve (2,016 to 2,036 ms). Timeline requests fell from 102 to 68 and transfer from 1,279.5 to 855.1 KiB, with LCP from 4,140 to 1,648 ms.
3. **Repeated forecast data and date work shrink.** Local benchmarks on identical inputs cut Stars night grouping from 6,641.52 to 46.11 ms and trip day grouping from 251.65 to 31.46 ms ([DATE-PERFORMANCE.md](../DATE-PERFORMANCE.md), [#7963](https://github.com/jomcgi-org/homelab/pull/7963)). Shared forecast hours cut Stars inline data from 9,271,811 to 3,093,449 bytes and script compile plus evaluation from 480.52 to 115.15 ms ([Stars PERFORMANCE.md](../src/routes/public/app/stars/PERFORMANCE.md), [#8000](https://github.com/jomcgi-org/homelab/pull/8000)). These are Node results; no live speedup is inferred.
4. **Heavy endpoints stay slow when uncached.** Unique-query p95 is 2,181 ms for Heat, 1,670 ms for Stars history and 1,155 ms for Stars data. [#8006](https://github.com/jomcgi-org/homelab/pull/8006), [#8018](https://github.com/jomcgi-org/homelab/pull/8018) and [#8019](https://github.com/jomcgi-org/homelab/pull/8019) change backend reads and encoding; the probes do not isolate them. The Stars Historical click median is 304 ms after [#7997](https://github.com/jomcgi-org/homelab/pull/7997) and [#8024](https://github.com/jomcgi-org/homelab/pull/8024), above the 200 ms target.
5. **Cache boundaries and asset retention are in code.** [#7967](https://github.com/jomcgi-org/homelab/pull/7967) keeps session, cookie and private responses out of shared caches. [#8020](https://github.com/jomcgi-org/homelab/pull/8020) and [#8025](https://github.com/jomcgi-org/homelab/pull/8025) archive public assets before deployment and serve retained ones afterwards. Their live acceptance is under [Not verified](#not-verified).

## Budgets and cache windows

Build budgets limit initial, deferred and worker gzip bytes separately. Core initial limits are 85,000 bytes for Hikes, Stars, trip views and the leaderboard, 80,000 for Ships and Scotland, and 75,000 for the Trips index and Notes. Map routes allow 330,000 deferred bytes and 170,000 worker bytes. These are build limits, not whole-page transfer totals. See the [configuration](../performance/bundle-budgets.json), [cache and budget procedure](public-app-cache-windows.md) and [measurement instructions](public-app-performance.md).

Only public, non-personalized responses may be cached. Session, cookie-dependent, private and friends responses are `no-store`. Each app's `PERFORMANCE.md` records its normal stale window and outage window separately from source-data age. The two windows are alternatives. The maximum source age went to Joe in [#7960](https://github.com/jomcgi-org/homelab/issues/7960).

## Changes measured

All 30 PRs are merged.

| PR | Change |
| --- | --- |
| [#7963](https://github.com/jomcgi-org/homelab/pull/7963) | Date formatting |
| [#7967](https://github.com/jomcgi-org/homelab/pull/7967) | Session cache boundary |
| [#7969](https://github.com/jomcgi-org/homelab/pull/7969) | Bundle budgets |
| [#7971](https://github.com/jomcgi-org/homelab/pull/7971) | Notes SSR overview |
| [#7972](https://github.com/jomcgi-org/homelab/pull/7972) | Hikes, Ships and Stars SSR snapshots |
| [#7974](https://github.com/jomcgi-org/homelab/pull/7974) | Trip photos and navigation |
| [#7975](https://github.com/jomcgi-org/homelab/pull/7975) | Measurement harness |
| [#7987](https://github.com/jomcgi-org/homelab/pull/7987) | Trip worker asset |
| [#7989](https://github.com/jomcgi-org/homelab/pull/7989) | Trip route previews |
| [#7991](https://github.com/jomcgi-org/homelab/pull/7991) | Ships incremental animation |
| [#7992](https://github.com/jomcgi-org/homelab/pull/7992) | Calibrated measurement evidence |
| [#7993](https://github.com/jomcgi-org/homelab/pull/7993) | App budgets and freshness docs |
| [#7994](https://github.com/jomcgi-org/homelab/pull/7994) | Leaderboard fixture readiness |
| [#7996](https://github.com/jomcgi-org/homelab/pull/7996) | Notes budget ratchet |
| [#7997](https://github.com/jomcgi-org/homelab/pull/7997) | Stars Historical interaction |
| [#7999](https://github.com/jomcgi-org/homelab/pull/7999) | Timeline visible photos |
| [#8000](https://github.com/jomcgi-org/homelab/pull/8000) | Stars forecast sharing |
| [#8002](https://github.com/jomcgi-org/homelab/pull/8002) | Navigation and endpoint probes |
| [#8004](https://github.com/jomcgi-org/homelab/pull/8004) | Screenshot public host routing |
| [#8005](https://github.com/jomcgi-org/homelab/pull/8005) | Ships unavailable Heat control |
| [#8006](https://github.com/jomcgi-org/homelab/pull/8006) | Ships Heat scalar reads and direct encoding |
| [#8008](https://github.com/jomcgi-org/homelab/pull/8008) | Stars click CPU diagnostic |
| [#8018](https://github.com/jomcgi-org/homelab/pull/8018) | Stars full-history direct encoding |
| [#8019](https://github.com/jomcgi-org/homelab/pull/8019) | Ships snapshot scalar reads and direct encoding |
| [#8020](https://github.com/jomcgi-org/homelab/pull/8020) | Archive public assets before deployment |
| [#8021](https://github.com/jomcgi-org/homelab/pull/8021) | Keep happy-dom observer callbacks through garbage collection |
| [#8022](https://github.com/jomcgi-org/homelab/pull/8022) | Page-cache measurement cohorts |
| [#8024](https://github.com/jomcgi-org/homelab/pull/8024) | Stars historical worker URL transport |
| [#8025](https://github.com/jomcgi-org/homelab/pull/8025) | Serve retained public assets |
| [#8026](https://github.com/jomcgi-org/homelab/pull/8026) | Prioritize the visible Trips day photo |

## Follow-up issues

These issues held the open work on 15 November. All five are now closed on GitHub.

| Issue | Outstanding decision or work |
| --- | --- |
| [#7960](https://github.com/jomcgi-org/homelab/issues/7960) | Approve maximum source freshness and stale and outage windows per app, including static Scotland, policy, Ember and redirect HTML. Keep their BYPASS until then. |
| [#7961](https://github.com/jomcgi-org/homelab/issues/7961) | Confirm the account-level edge bypass for identity and session requests; repo header tests cannot establish every account rule. |
| [#7988](https://github.com/jomcgi-org/homelab/issues/7988) | Restore the four original Frank images or their host. |
| [#7998](https://github.com/jomcgi-org/homelab/issues/7998) | Accept retained-asset fallback across deployments: historic generations, held old-HTML browser sessions, per-pod mixtures and lifecycle duration. |
| [#7962](https://github.com/jomcgi-org/homelab/issues/7962) | First-MISS origin cost, seven slow uncached endpoints, the Stars Historical click, Trips and Grimoire LCP, physical-device, worker and field-INP evidence, and dynamic-route coverage. |

## Not verified

These were measured or checked, but the evidence was never published or needs a GitHub sign-in:

- PostgreSQL fixture plans and execution times for the Ships and Stars queries.
- Rollout checks: which release each run measured, the archive PreSync job, and post-rollout checks of the screenshot service, the no-WebGL Trips and Ships fallbacks and the Stars production payload size.
- Immutable-asset retention captures for #8020 and #8025, including their 503 and 429 responses.
- Intermediate hosted runs between the baseline and the final run, including the keyed-before reference and earlier Ships and Stars MISS series.
- Final-run sample detail: first-visit and worst-sample timings (including first-MISS header arrival and LCP for Hikes, Ships and Stars), endpoint maxima, HIT-only splits, decoded sizes, request chains, Notes late LCP candidates and the Stars click CPU profile. These sit in the final artifact's `results.json` and `stars-diagnostic/`.
- The combined local integration test run.

## Evidence

Five committed files back the figures above. Each is a byte-for-byte copy from a workflow artifact, and the [evidence manifest](performance/2026-11-15-evidence-manifest.json) records their SHA-256.

| File | Bytes | Backs |
| --- | ---: | --- |
| [`2026-11-15-final-run/table.md`](performance/2026-11-15-final-run/table.md) | 5,068 | Final journey and endpoint medians |
| [`2026-11-15-final-run/provenance.json`](performance/2026-11-15-final-run/provenance.json) | 1,303 | Final origin, harness, page-cache key, browser and runner |
| [`2026-11-15-final-run/calibration.json`](performance/2026-11-15-final-run/calibration.json) | 17,700 | Latency calibration |
| [`2026-11-14-hosted-baseline-run/table.md`](performance/2026-11-14-hosted-baseline-run/table.md) | 2,823 | Baseline journey and endpoint medians |
| [`2026-11-14-hosted-baseline-run/provenance.json`](performance/2026-11-14-hosted-baseline-run/provenance.json) | 886 | Baseline harness, browser and runner |

Readers signed in to GitHub can download the source artifacts ([baseline](https://github.com/jomcgi-org/homelab/actions/runs/43652846917/artifacts/14137481532), [final](https://github.com/jomcgi-org/homelab/actions/runs/43671642559/artifacts/14147409580)) until they expire 90 days after each run. The manifest records each ZIP's SHA-256. The [HTTP baseline](performance/2026-11-14-baseline.md) is a separate curl measurement and is not compared here.
