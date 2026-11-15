# Public map first views

Hikes, ships and stars render their controls and six snapshot records in the
initial HTML. The records occupy the reserved map area. The map replaces them
only after its GeoJSON source is loaded and a render event has fired. Failed
MapLibre imports or WebGL initialization retain the records and explain why
the interactive map is unavailable.

The full datasets and map filters are unchanged. Hikes shows distance, ascent
and duration with the existing WalkHighlands links. Ships shows vessel names,
types, speeds and destinations. Stars shows named sites or their coordinates
and forecast hours. A historical stars link retains an explicitly labelled
forecast snapshot until historical data arrives and paints.

Ships heat and stars history warm in idle time after the populated map paints.
Selecting those modes still starts their request immediately. Hike detail and
vessel tracks remain on demand.
The ships mode, filter and detail controls stay above the retained snapshot,
so a failed initial heat request can be recovered by returning to vessels.

Hikes and stars serialize `renderedAt` with the page data. SSR and hydration
start from the same clock, including a cached response. Mount then advances to
the visitor's present time; the existing refresh timers continue afterwards.
These two page loaders leave HTML validators to SvelteKit instead of forwarding
the backend's data-only ETag or Last-Modified value.

## Local evidence

Measured on 2026-11-14 with production snapshots captured that evening: 1,653
walks, 7,786 vessels and 14,234 forecast sites. Three direct Svelte server renders
per route, median elapsed time, using the same inputs and a fixed rendering
clock. The stars result includes the date-formatting optimization in `3268792`,
which must land before this change deploys.

| Route | Visible records before / after | Component HTML bytes before / after | Local SSR median before / after |
| --- | ---: | ---: | ---: |
| Hikes | 0 / 6 | 259 / 7,886 | 0.28 / 4.79 ms |
| Ships | 0 / 6 | 1,537 / 5,344 | 0.19 / 1.15 ms |
| Stars | 0 / 6 | 224 / 6,559 | 0.09 / 43.93 ms |

These are advisory container measurements of component rendering. They exclude
inline snapshot serialization, layout HTML, compression, network transfer and
browser work. They establish that real content now exists in SSR and quantify
its server cost. They do not establish a production LCP or interaction speedup.

The route tests cover visible snapshot records without a client fetch, bounded
row counts, unnamed forecast cells, cached clocks, the populated-frame handoff,
initialization failure, failed heat-view recovery, deferred warming and
historical deep links. Existing
filter, detail, theme, cleanup and loader tests also pass. Run them with:

```sh
pnpm exec vitest run src/routes/public/app/hikes src/routes/public/app/ships src/routes/public/app/stars
```

The repository's `//projects/monolith/frontend:test` target includes these tests.
The static stars fixture also declares the shared snapshot component as an
input. The public production build passed with the changed map components.
Initial route JavaScript gzip grew from 72,096 to 73,052 bytes for hikes,
68,017 to 69,171 for ships, and 72,636 to 74,066 for stars. MapLibre and its
worker remain deferred at 285,621 and 146,653 gzip bytes respectively. Live
mobile timing and rollout verification remain separate acceptance checks.
