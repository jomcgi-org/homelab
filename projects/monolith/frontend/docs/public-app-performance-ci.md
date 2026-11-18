# Hosted public app measurements

The `Public app performance` GitHub Actions workflow runs the
[measurement script](public-app-performance.md) against the currently deployed
`https://jomcgi.dev`. It starts for pull requests that change the script, its
metric helpers or the workflow. After the workflow lands on main, run it again
from the Actions page or with:

```sh
gh workflow run public-app-performance.yml --ref main
```

It measures the 15 routes from the HTTP baseline: hikes, ships, trips,
Scotland, the leaderboard, notes, Frank, the Firecracker redirect and its
destination, Scotland's three policy/support pages, and a trip summary,
timeline and day. The fixed representative public trip is
`2025-liard-hot-springs`, with day 1; its slug is recorded in provenance. Each
route gets three new anonymous browser contexts, with the documented mobile
profile and a 15-second observation window after DOMContentLoaded. Nine fixed
public data URLs get 20 sequential GET samples each. These cover the three
primary SvelteKit data envelopes, one Hikes walk, one Ships track, ships heat,
notes graph and stats, and Scotland marketplace. Other trip slugs and protected
Grimoire corpus journeys remain separate measurements. A sixteenth route,
`/app/grimoire`, observes only the logged-out admission page with three new
contexts. It has no earlier browser baseline and no scripted interaction.
A challenge or failure is recorded, not solved; its timings do not measure
protected corpus content.

The origin, routes, sample counts and observation window are fixed in the
workflow. It supplies only the four read-only interaction selectors described
below, with no initial cookies or account session, and never purges the CDN. It uses a hosted Ubuntu runner, pinned Node and
Playwright versions, a read-only repository token and no secrets or deployment
credentials. Only one measurement workflow runs at a time. The measurement
command has a 26-minute deadline within a 28-minute step and a 45-minute job;
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

Pull-request runs explicitly select a keyed page cohort to avoid waiting for
an earlier release's canonical HTML cache lifetime. The workflow generates
`run-<run-id>-attempt-<attempt>` once, passes it as `--page-cache-key` to the
benchmark, and repeats it across all page runs. Manual
runs remain canonical unless `fresh_page_cache=true` is explicitly selected:

```sh
gh workflow run public-app-performance.yml --ref main -f fresh_page_cache=true
```

Provenance records `pageCache` (mode, parameter and key); page and diagnostic
JSON retain each exact requested URL, logical route and actual edge headers.
This does not change any endpoint series or make every request a MISS. A
redirect that drops the key is marked `key-not-retained`, not a fresh-key
destination. Inspect first-run and worst-run values as well as medians, and
verify served assets after rollout. Canonical and keyed cohorts must stay
separate; a cross-cohort difference is not an isolated code effect. The local
hosted `page-cohort-fixture.json` checks three identical keyed page requests,
unchanged natural data URLs, ordinary redirects and excluded recovery reloads.
It makes no assertion about production cache behavior.

This is a measurement job, not a latency threshold gate. Public response
correctness, cache isolation and route bundle budgets remain separate CI
checks. A successful observation run means its requested samples completed;
it does not mean production met the performance targets.

The hosted command now selects visible data rows for Hikes and Ships,
Notes overview rows, the trip summary's data-derived date range (`.dateline`),
and the day's photo element (`.photo img`). The trip selectors do not depend
on the route preview. Photo
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
control, Ships Heat mode, and the representative day's
Next photo button. It records synthetic Event Timing separately from INP.
Other routes currently have no scripted interaction sample. The 26-minute
command deadline is unchanged; partial observations are still uploaded if a
requested control fails or the run times out.

Pull request validation also runs 20 unique-query GETs for each of the same
nine audited public endpoints, at most 180 additional reads. This validates
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
reads and saves `navigation-fixture.json`. It also interrupts an in-progress
observation with a document replacement after a module 404, checking that
collected request statuses survive the error path.

Stars moved to the jomcgi.dev site (HOME-210), so this workflow no longer
measures it. The site's `:stars_browser_test` covers the page.
