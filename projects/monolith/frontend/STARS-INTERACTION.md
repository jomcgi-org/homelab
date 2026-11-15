# Stars historical interaction

The historical snapshot is immutable after loading: `loadHistory` replaces the
whole array, and month projection creates new rows without changing the fetched
rows or their twelve-month arrays. It now uses Svelte's raw state. Previously,
the first Historical click created deep reactive proxies while reading every
site's monthly values. Later month changes also paid for those proxy reads.

The map also remembers the last prepared collection sent to each source. The
existing effects still update filters, selection and the site index, but effects
observing the same change no longer submit identical geometry repeatedly.
New collections and replacement sources still receive their full data. Both
point and cell layers retain every qualifying site and the same scores.

## Evidence

The hosted production run beginning 2026-11-15 at 00:01:52 UTC observed
Historical button Event Timing durations of 1,360, 1,144 and 1,152 ms. Its median
was 1,152 ms. That run served the older page generation and did not capture a
CPU trace or requests after the click. It establishes a slow interaction,
not the exact attribution or a post-change result. Field INP remains unknown.

Local measurements compare `a30c8b27f720bb987e1111392dede81538f457ff` with this
change, using Node 24.22.1, Svelte 5.56.9 and the composed Happy DOM route test.
Both captured public payloads contain 14,234 sites. Each sample starts a fresh
Vitest process, mounts the real page and map components with the existing
MapLibre double, and warms the history request after the initial map render.
Timing spans the click and its Svelte updates, using twelve awaited ticks.
The clock is fixed at 2026-11-14 22:37:19 UTC. Results are medians of three
processes, without CPU throttling or a browser worker.

| Operation | Before, ms | Raw snapshot only, ms | Final change, ms |
| --- | ---: | ---: | ---: |
| First Historical click after history fetch | 1,649.18 | 52.55 | 64.26 |
| Historical after switching back to Live | 616.89 | 45.40 | 34.85 |
| January selection | 292.01 | 73.76 | 42.36 |
| Submissions to each source on first Historical click | 3 | 3 | 1 |

The mock does not simulate worker serialization or rendering, so the final
deduplication is evidenced by submission counts, not an isolated timing claim.
The three final first-click samples were 60.42, 64.26 and 65.59 ms; the baseline
samples were 1,649.18, 1,718.07 and 1,220.66 ms.

Every before/after sample produced the same 14,234 historical cells, including
their IDs, complete polygon coordinates and scores. The SHA-256 of the
serialized cell FeatureCollection was
`3e3f655a7106e15647736fe9ec897b5cf6248105140fca3dbed12499c72d11bb`.
The input response SHA-256 hashes are:

- Historical map: `bc3c1ee178dc460229593fedef804fddd4eaaeab6678fcabf0d4904cd03b3ec8`
- Forecast navigation data: `3994970a964ac2b8244a78f597fc81b64b19ed0eb4af21b24386254595451ad2`

This changes no response fields, requests or freshness policy. Mobile response
time and production interaction latency must be measured after deployment.

## Regression coverage

The route tests cover the immutable snapshot boundary without a machine-speed
threshold, all twelve months and All year, site selection and its monthly
chart, repeated mode changes, fetch reuse, one submission per prepared source,
and a replaced forecast snapshot with every refreshed site selectable. Existing
tests retain loading, failure/retry, URL controls, clock updates, SSR and
first-view data assertions. The 83 focused tests pass. Reinstating deep state
fails the snapshot identity assertion; reinstating unconditional submissions
fails the history and refreshed-snapshot write-count assertions.

Run from the frontend directory:

```sh
node node_modules/vitest/vitest.mjs run src/routes/public/app/stars src/lib/public/stars
```
