# Hosted public app measurements

The `Public app performance` GitHub Actions workflow runs the
[measurement script](public-app-performance.md) against the currently deployed
`https://jomcgi.dev`. It starts for pull requests that change the script, its
metric helpers or the workflow. After the workflow lands on main, run it again
from the Actions page or with:

```sh
gh workflow run public-app-performance.yml --ref main
```

It measures the 16 routes from the HTTP baseline: hikes, ships, stars, trips,
Scotland, the leaderboard, notes, Frank, the Firecracker redirect and its
destination, Scotland's three policy/support pages, and a trip summary,
timeline and day. The fixed representative public trip is
`2025-liard-hot-springs`, with day 1; its slug is recorded in provenance. Each
route gets three new anonymous browser contexts, with the documented mobile
profile and a 15-second observation window after DOMContentLoaded. Thirteen fixed
public data URLs get 20 sequential GET samples each. These cover the four
primary SvelteKit data envelopes, one Hikes walk, one Ships track, stars map,
history and history map, ships heat, notes
graph and stats, and Scotland marketplace. Other trip slugs and protected
Grimoire corpus journeys remain separate measurements. A seventeenth route,
`/app/grimoire`, observes only the logged-out admission page with three new
contexts. It has no earlier browser baseline and no scripted interaction.
A challenge or failure is recorded, not solved; its timings do not measure
protected corpus content.

The origin, routes, sample counts and observation window are fixed in the
workflow. It supplies only the four read-only interaction selectors described
below, with no initial cookies or account session, and never purges the CDN. It uses a hosted Ubuntu runner, pinned Node and
Playwright versions, a read-only repository token and no secrets or deployment
credentials. Only one measurement workflow runs at a time. The measurement
command has a 26-minute deadline within a 28-minute step and a 40-minute job;
failed samples fail the step. No application build or deployment runs here.

The run summary contains the median table. The 90-day artifact contains
`results.json`, `table.md`, `measurement.log` and `provenance.json`. The latter
records the measurement-tool SHA, workflow run and attempt, Node, Playwright,
Chromium and runner details. The measured application is whatever production
served during that run. `deployedSha` deliberately stays null: independently
verify and record the rollout revision before making a before/after claim.

Artifacts upload after a measurement failure, including completed partial
results where available. A timeout can leave only partial JSON and no table;
missing measurements are not passes. Hosted runner location and hardware can
vary, so retain provenance and compare matching browser/profile settings.
The workflow observes actual edge state rather than guaranteeing a cold CDN.
Worker pages cannot certify the full throttled profile, and synthetic loading
does not establish field INP. These limitations remain explicit in the report.

This is a measurement job, not a latency threshold gate. Public response
correctness, cache isolation and route bundle budgets remain separate CI
checks. A successful observation run means its requested samples completed;
it does not mean production met the performance targets.

The hosted command now selects visible data rows for Hikes, Ships and Stars,
Notes overview rows, the trip summary's data-derived date range (`.dateline`),
and the day's photo element (`.photo img`). The trip selectors exist before
the route-preview change and do not depend on that pending deployment. Photo
element visibility does not prove image decoding or map readiness. A selector absent from the deployed release remains
unavailable; it does not become a successful first-paint result. These are
DOM milestones, not proof of painted or interactive maps. The same observer
first runs against local browser fixtures: an element whose own rectangle is
inside the viewport but is entirely clipped by its ancestor, and CSS-only
opacity and position animations. Their result is saved as
`observer-fixture.json`. The checks do not contact production. The same pinned browser also compares
three pairs of unthrottled and 150 ms throttled local HTTP navigations, saving
header-event timing, raw NavigationTiming and pass criteria in `calibration.json`.

After each load observation is frozen, the workflow clicks the Hikes limits
control, Ships Heat mode, Stars Historical mode, and the representative day's
Next photo button. It records synthetic Event Timing separately from INP.
Other routes currently have no scripted interaction sample. The 26-minute
command deadline is unchanged; partial observations are still uploaded if a
requested control fails or the run times out.

Pull request validation also runs 20 unique-query GETs for each of the same
thirteen audited public endpoints, at most 260 additional reads. This validates
the probe series on the hosted browser alongside the page observations, using
the existing read-only job and fixed endpoint allowlist. Provenance records
`cacheMissProbesEnabled` and the planned endpoint/sample counts.

For manual runs, the `cache_miss_probes` workflow-dispatch input still defaults
to false. To request that same bounded series explicitly:

```sh
gh workflow run public-app-performance.yml --ref main -f cache_miss_probes=true
```

Normal-key and probe distributions remain separate, including observed edge
statuses. A unique key does not guarantee an edge MISS or a cold backend.
The existing 26-minute command deadline still applies; incomplete samples
are retained where available and cannot count as passes.

The runner records per-sample UTC timestamps and browser time origins.
Multiple top-level document requests, including automatic chunk-recovery
reloads, exclude that sample from medians and fail the requested run count.
The local fixture verifies redirect versus reload tracking before production
reads and saves `navigation-fixture.json`.

For Stars, the workflow waits up to 20 seconds after freezing load metrics
for the naturally requested history-map response to finish, followed by two
animation frames. Readiness and waiting time are explicit in JSON. This does
not fetch data, prove map paint, or extend the load observation. If the
response does not finish, the raw click duration remains recorded but is
excluded from comparable interaction medians. A fast loading-state click
cannot substitute for a click that renders already-loaded history.
