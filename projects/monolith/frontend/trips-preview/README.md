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
viewer and map states. `checks.json` records the source head, dimensions,
scheme and measured document overflow. All remote requests are fulfilled
locally or rejected. MapLibre uses its real WebGL renderer, production
controls and layers on a blank local basemap and synthetic terrain tile.
This does not validate production tile geography or real-host interaction.
The image swatches exercise the existing gallery/display URL selection,
decoding, keyboard and focus wiring, not photographic colour fidelity.

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
