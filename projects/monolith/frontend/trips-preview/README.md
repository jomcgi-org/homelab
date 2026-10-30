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
the fixture in required Linux CI. The shared `Fixture preview build` workflow
from #7220 now invokes this same browser runner and requires all 32 PNGs. It
records source/runner SHA, browser versions, image hashes and failure traces,
then uploads `fixture-preview-evidence-<full-head-sha>` separately from the
static public artifact. Evidence requests 90-day retention, bounded by the
repository policy. Its aggregate includes an offline PNG gallery, with no map
JavaScript or worker published and no production/backend connection.

The comparison displays the actual PR head and merge base. A missing or changed
historical harness is explicitly unavailable; it never substitutes another
revision. These synthetic screenshots do not certify geographic tiles or
actual-host acceptance.

The operator landing hold remains in force after evidence and CI pass.
Screenshots remain a repository-acceptance requirement, separate from
post-merge rollout and real-host acceptance on #7141.
