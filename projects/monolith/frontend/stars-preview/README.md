# Stars repository evidence

This fixture mounts the production root layout, public layout, page wrapper,
StarsApp and StarsMap. Forecast/history rows are invented in
`test/stars/fixtures.js`. The clock is fixed to October 19, 2026 at noon UTC.
MapLibre, its WebGL renderer and OpenFreeMap's vector geography are real.
The browser check requires access to `tiles.openfreemap.org`; no credentials
or production API are used. The local font is the same vendored font as the
existing fixture preview.

From `projects/monolith/frontend`:

```sh
STARS_PREVIEW_SHA=$(git rev-parse HEAD) pnpm exec vite build --config stars-preview/vite.config.js
python stars-preview/browser_check.py --artifact stars-fixture-preview --output /tmp/stars-evidence
```

The Python environment needs Playwright 1.63.0 and Chromium with its Linux
dependencies (`playwright install --with-deps chromium`). The check saves the
eight live/historical, 390/1440, light/dark captures, each with a selected site
and expanded filter, plus overflow, keyboard, paint and contrast measurements.
The preview embeds `STARS_PREVIEW_SHA`; the browser runner rejects an unstamped
or stale build and records that source SHA and its Chromium version in every
case. `--expected-sha` can additionally guard the checkout. The contrast check
waits for rendered color transitions and composites each element's background.
The runner accepts `--chromium /absolute/path/to/chromium` for a local browser
when the pinned Playwright download cannot run in the guest. Record that
browser version as advisory evidence, not a pinned CI run.
The shared `Fixture preview build` workflow from #7220 now invokes this
same checker and requires all eight PNGs. Its CI/public capture uses an
offline profile with an invented blank basemap and an explicit capture caption;
it does not request OpenFreeMap tiles or certify real geography. The manual
commands above retain the owner's real-tile profile for separate local checks.

Shared evidence records source/runner SHA, browser versions, PNG hashes,
measurements and traces in `fixture-preview-evidence-<full-head-sha>`, with
requested 90-day retention bounded by repository policy. The public aggregate
contains a PNG-only gallery under the unchanged CSP, with no map JavaScript,
worker or backend connection. Its comparison displays actual source and merge
base SHAs; an absent or changed historical harness is explicitly unavailable.
This tooling integration leaves the owner's implementation and landing hold
unchanged. Generated output remains outside Git.

The checker samples the actual MapLibre WebGL canvas during a render and
compares unobscured background pixels with the independent sheet contract:
RGBA `[255, 255, 255, 255]` in light and `[24, 26, 32, 255]` in dark. It checks
the initial scheme and four flips, alongside camera/source/canvas identity.
The offline profile supplies the blank geography; production CSS, palette
resolution and rendering remain real. A real-tile profile needs unobscured
background samples too. `checks.json` records samples, expected colors, the
build stamp, invocation, source SHA and browser version.

The DOM tests mock the MapLibre boundary and color rasterizer for deterministic
lifecycle assertions. They do not supply screenshot evidence. The browser
check runs the actual map and production CSS, including repeated scheme flips.
Neither check performs SvelteKit server loads or verifies the public host.
