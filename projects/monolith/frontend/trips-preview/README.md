# Trips sheet fixture

This fixture mounts the production root layout, public layout, trips index,
trip summary, day and timeline. The route, metadata and image swatches are
invented in `test/trips-fixtures.js`. No server load, API, private data or
credentials enter the preview. It is repository evidence, not live acceptance.

After the frozen workspace dependency install, from the frontend directory:

```sh
pnpm exec vitest run src/routes/public/app/trips src/lib/trips
pnpm exec vite build --config trips-preview/vite.config.js --outDir /tmp/trips-preview
python3 trips-preview/browser_check.py --artifact /tmp/trips-preview --output /tmp/trips-evidence --head "$(git rev-parse HEAD)"
```

The browser script uses the existing Python Playwright 1.63.0 runner. It
captures all four pages at 390 and 1440 pixels, light and dark, plus timeline
viewer/map and day map-controls/fullscreen states. `checks.json` records the
source head, browser version, dimensions, scheme and measured document overflow
for every PNG. Its `complete` flag is true only after all 16 combinations pass.
All remote requests are fulfilled
locally or rejected. MapLibre uses its real WebGL renderer, production
controls and layers on a blank local basemap and synthetic terrain tile.
This does not validate production tile geography or real-host interaction.
The image swatches exercise the existing gallery/display URL selection,
decoding, keyboard and focus wiring, not photographic colour fidelity.

The current basemap repaint uses `--paper` for water, park, wood, landuse and
building fills over `--sheet`. Real water/land contrast remains unverified.
Liberty's `natural_earth` raster and `building-3d` fill-extrusion keep their
upstream paints. The blank fixture cannot show these layers or certify their
light/dark appearance; its `checks.json` carries that limitation. Inspect
real Liberty and Positron tiles during the retained live check on #7141.

A complete capture has 32 PNGs: 16 page/width/scheme combinations, eight
timeline viewer/map states and eight day map-controls/fullscreen states.
Build and capture at the exact PR head and link the viewable PNGs and
`checks.json` in the PR. Any source or CI-format commit invalidates that
exact-head evidence and requires a new build and capture.

Chromium needs usable sans-serif and monospace system fonts as well as its
shared libraries. The capture checks their measured text widths before taking
evidence: a fontless browser can render empty labels and falsely report zero
overflow. `font_widths_px` records that preflight. Inspect every captured image;
passing browser assertions alone does not establish visual acceptance.

The fixture-only MapLibre adapter bundles the module worker explicitly and
exposes real map instances to the browser check. Captures wait for loaded,
stationary maps, a fresh idle render and browser presentation frames. They
reject map errors and require rendered route features. Readiness predicates
alone had allowed a stale canvas frame beside an updated DOM photo marker.
`maps` in each check records the layer IDs and rendered-route counts. A canvas
element alone is insufficient: without the worker bundle, a blank map could
pass the old capture checks. Production imports and map options are unchanged.

`//projects/monolith/frontend:test` includes the composed DOM tests and the
existing timeline lifecycle suite. `:trips_fixture_preview_build_test` builds
the fixture in required Linux CI. No active workflow invokes this browser
runner. The blog and factory preview jobs do not establish trips acceptance.

`ci-evidence.workflow.yml` is an inactive workflow template for an authorized
dispatcher to install as `.github/workflows/trips-fixture-evidence.yml`.
GitHub refused the correction runner's workflow push because its credential
lacks `workflow` scope. The template does not execute from this directory,
and staging it does not satisfy the screenshot requirement. Activation needs
an authorized workflow writer; do not change credentials to work around the
refusal.

The proposed job checks out the exact PR head with read-only permissions and
no persisted Git credentials, runs the targeted tests, builds this fixture
and installs the pinned browser with its system libraries. It uploads all 32
PNGs and `checks.json` as `trips-fixture-evidence-<full-head-sha>` for 14 days,
including partial evidence on failure. A failed capture keeps its job red.

After activation, link the workflow run and its downloadable artifact in the
draft PR. Verify
`checks.json` has the current head, `complete: true`, all 16 combinations and
zero overflow for all 32 PNGs, then inspect the images. Rebuild and recapture after
any new source or CI-format commit; expired artifacts also need fresh capture.
Generated output stays outside Git. The template has no publisher, Pages
deployment or connection to the `Fixture preview build` publisher trigger.
The operator landing hold remains in force after evidence and CI pass.
Screenshots remain a blocking repository-acceptance requirement, separate
from post-merge rollout and real-host acceptance on #7141.
