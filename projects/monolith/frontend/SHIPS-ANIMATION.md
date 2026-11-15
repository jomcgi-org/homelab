# Ships animation work

The map updates moving vessel geometry at most four times a second. Each
animation update contains only positions whose extrapolated coordinates can
change. Stationary vessels and fixes already at the existing 300-second limit
remain in the source. The tick crossing the limit includes its final position.
Snapshot refreshes, selection and hover still replace the full source.

The installed MapLibre 6.9.0 supports
`updateData({ update: [{ id, newGeometry }] })` for sources with unique
feature IDs. The fleet's MMSI keys supply those IDs.
Geometry updates retain every feature's properties, including selection and
hover. The source and worker implementations are in
`maplibre-gl/src/source/geojson_source.ts`, `geojson_source_diff.ts` and
`geojson_worker_source.ts` in the installed package, resolved from the declared
`^6.4.1` range. The library merges queued diffs; a full `setData` replaces
pending diffs.

When every moving fix has reached the cap, a time comparison skips animation
submissions entirely. A fresh fix resumes them. The comparison and geometry
calculation preserve existing handling of future-dated fixes and clock
changes. The animation cadence and public data requests are unchanged.

## Local measurements

Two complete public snapshots were evaluated at their response timestamps.
No vessels were removed. The baseline uses source commit
`70e8c09e44eb5eda5b978156718bb7ffea3e0a0f`.

| Snapshot | Active | Capped moving | Stationary | Total |
| --- | ---: | ---: | ---: | ---: |
| Saved baseline, 2026-11-14 22:36:54 UTC | 132 | 4,900 | 2,754 | 7,786 |
| Fresh production HIT, 2026-11-14 23:36:39 UTC | 104 | 4,915 | 2,767 | 7,786 |

The fresh response was fetched once from `/app/ships/__data.json`, without
cookies or authorization. Its latest fix was at 23:35:55.226 UTC. The saved
baseline's latest fix was at 22:36:47.076 UTC. SHA-256 hashes of the navigation
data responses are:

- Saved baseline: `f488c91393920cf77ae67017324a5e9e381002b7981115117999cb7867e467fa`
- Fresh response: `40ee85124646917c6afb759604a5757e925c3161e68197be2018aa152ded4c53`

Node 24.22.1 evaluated the component's feature and diff builders for a tick
250 ms after each response timestamp. Each result below is the median of
three independent processes. Each process measured 15 warmed batches of 100
construction calls, nine batches of 10 full-object clones, and 15 batches of
100 diff clones. `structuredClone` is a local serialization-cost proxy; the
benchmark does not run a browser or MapLibre worker.

| Measurement | Saved baseline before | Saved baseline after | Fresh response before | Fresh response after |
| --- | ---: | ---: | ---: | ---: |
| Update construction, ms | 0.972 | 0.094 | 1.075 | 0.086 |
| Node clone proxy, ms | 13.677 | 0.109 | 13.192 | 0.088 |
| Serialized update object, bytes | 1,644,214 | 13,745 | 1,644,241 | 10,817 |
| Features included in an animation update | 7,786 | 132 | 7,786 | 104 |
| Features retained in the source | 7,786 | 7,786 | 7,786 | 7,786 |
| Animation submissions per second | 4 | 4 | 4 | 4 |

A separate 25-second tick sequence on the fresh response measured 0.924 ms
per baseline submission and 0.085 ms per diff submission. It retained the
same four-updates-per-second cadence. JSON sizes describe the update objects;
they do not measure browser wire bytes. HTTP transfer is unchanged. Worker
processing, mobile frame timing and production responsiveness remain
unverified.

The first idle experiment evaluated the saved baseline at 23:28:02.408 UTC,
when all its moving fixes were capped. That aged fixture reduced unchanged
source submissions from four per second to zero. It does not represent the
active production feed. An idle-only guard on the fresh response retained
all submissions and measured 0.924 ms before versus 1.044 ms after; geometry
diffs provide the active-feed reduction reported above.

Full-corpus comparisons apply the updates with MapLibre's own
`toUpdateable` and `applySourceDiff` functions and compare every resulting
feature to the original full rebuild. They cover active ticks, the final cap
transition, reversed time and selection/hover changes. Route DOM regressions
verify unchanged features and properties, idle ticks, final positions,
immediate interaction updates, and resumed animation after replacing a
snapshot. Existing ship geometry, server, SSR and route tests remain green.
