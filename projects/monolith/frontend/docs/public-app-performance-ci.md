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
profile and a 15-second observation window after DOMContentLoaded. Ten fixed
public data URLs get 20 sequential GET samples each. These cover the four
primary SvelteKit data envelopes, stars map and history map, ships heat, notes
graph and stats, and Scotland marketplace. Other trip slugs and protected
Grimoire admission journeys remain separate measurements.

The origin, routes, sample counts and observation window are fixed in the
workflow. It supplies no interaction selectors, cookies or account session,
and never purges the CDN. It uses a hosted Ubuntu runner, pinned Node and
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
Notes overview rows, and route-preview captions for the representative trip
summary and day. A selector absent from the deployed release remains
unavailable; it does not become a successful first-paint result. These are
DOM milestones, not proof of painted or interactive maps.

After each load observation is frozen, the workflow clicks the Hikes limits
control, Ships Heat mode, Stars Historical mode, and the representative day's
Next photo button. It records synthetic Event Timing separately from INP.
Other routes currently have no scripted interaction sample. The 26-minute
command deadline is unchanged; partial observations are still uploaded if a
requested control fails or the run times out.
