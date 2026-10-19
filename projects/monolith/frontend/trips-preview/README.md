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

`//projects/monolith/frontend:test` includes the composed DOM tests and the
existing timeline lifecycle suite. `:trips_fixture_preview_build_test` builds
the fixture in Linux CI. Browser capture needs a runner with Chromium's system
libraries. Its output is the viewable PNGs and `checks.json`; publish those
through the repository's fixture-evidence artifact workflow and link the
exact-head artifact in the draft PR. That workflow currently captures the blog
fixture and does not yet invoke this trips runner. Connecting this runner to
it requires workflow-write authorization. Generated output stays outside Git.
The operator landing hold remains in force, and screenshots remain a blocking
repository-acceptance requirement until captured and inspected.
