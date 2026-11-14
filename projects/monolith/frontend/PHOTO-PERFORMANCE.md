# Trip photo loading

The day viewer previously requested five neighbouring photos at hydration,
then another every 100 ms until the entire day was downloaded. This started
while its visible photo was still decoding and the map was loading.

The viewer now waits for the visible photo to decode and the populated map to
render. It warms the next two photos and the previous photo, one at a time
at low priority. A changed selection cancels the pending optional image and
warms the new neighbourhood. Save-Data disables this optional work. Every
photo remains available through the existing scrubber and fullscreen view.

The first cover on the trips index is eager and high priority. Later covers
remain lazy. Image URLs, dimensions and payloads are unchanged.

## Measured request scheduling

Measured on 2026-11-14 using the real day component, its existing composed DOM
test harness and a 30-photo fixture. The Image double records assigned URLs;
the visible image's decode promise remains pending. Fake interval timers
advance the original background schedule deterministically. These counts
exclude map assets and do not measure browser wire traffic or LCP.

| State | Before | Changed |
| --- | ---: | ---: |
| Image URLs requested at hydration | 6 | 1 |
| URLs after 1 second with the visible photo still pending | 16 | 1 |
| URLs after 3 seconds with the visible photo still pending | 30 | 1 |
| Total URLs after readiness and completed neighbour loads, with no interaction | 30 | 3 |

The original schedule was measured at
`1f536d94934ccfef73aa3a2129f00be60f8a44ca`. The changed schedule is asserted by
`pages.dom.test.js`, including both readiness orders, source loading, serial
requests, navigation cancellation, unmount, decode failure and Save-Data.

The public production snapshot had 288 photos on day one of
`2025-liard-hot-springs` and 4,838 photos across the trip. One sampled day-one
display image was 67,324 bytes. No total gallery size is inferred from that
single image. Post-deploy mobile LCP and transfer remain separate acceptance
measurements.

Run the existing frontend tests with:

```sh
cd projects/monolith/frontend
node node_modules/vitest/vitest.mjs run src/routes/public/app/trips src/lib/trips
```

The frontend `:test` target already includes these tests and their map double.
