# Campsites sheet fixture

This preview mounts the production campsites page, map, root layout and public
layout. The parks and forecasts are invented. MapLibre renders the real
OpenFreeMap liberty basemap and the production heatmap and pin layers. The
preview subclass only exposes the real map to browser assertions. No backend,
credentials or production campsite data are used.

From `projects/monolith/frontend`, after `pnpm install --frozen-lockfile`:

```sh
pnpm exec vite --config campsites-preview/vite.config.js --port 4173
```

With Playwright, Chromium, system fonts and browser dependencies installed:

```sh
node campsites-preview/browser-check.mjs /absolute/path/to/playwright/index.mjs /absolute/path/to/evidence
pnpm exec vitest run src/routes/public/app/campsites
```

To exercise the built bundle and emitted MapLibre worker (the recorded
captures use this path), start this server instead of the dev server:

```sh
pnpm exec vite build --config campsites-preview/vite.config.js --outDir /tmp/campsites-preview-dist
pnpm exec vite preview --config campsites-preview/vite.config.js --outDir /tmp/campsites-preview-dist --port 4174
CAMPSITES_PREVIEW_URL=http://127.0.0.1:4174 node campsites-preview/browser-check.mjs /absolute/path/to/playwright/index.mjs /absolute/path/to/evidence
```

The browser check captures 390 x 1000 and 1440 x 1000 viewports in light and
dark, using full-page screenshots to include the open controls and forecast.
It checks actual map pin hit testing, list selection, source/layer retention
across scheme changes, layer visibility, pan/zoom, filters, all three sorts,
night URL updates and hydration, empty data, legend dismissal, keyboard focus,
scheme persistence and document overflow with controls closed and open.
Navigation composes engineering, legacy Ember, a legacy Nav surface and the
campsites page without unloading stylesheets. Legacy paint is compared with a
fresh load. `checks.json` records each viewport and scheme.

The clock is fixed at October 19, 2026, 12:00 UTC. Schibsted Grotesk is loaded
from the existing repository font; Google Fonts requests return empty CSS.
OpenFreeMap tiles, glyphs and sprites require public network access and can
change independently of this fixture. Chromium uses software WebGL. The
history adapter is not SvelteKit routing or server navigation. The refresh
adapter counts invalidations without fetching a backend; DOM tests execute
the production interval callback and check timer cleanup. Server tests retain
the timeout, cache headers and validators.

Generated bundles are not committed. Evidence captures belong outside the
production `:src` and `:src_public` inputs. Browser evidence does not establish
rollout, real-host routing, cache acceptance or live campsite availability.
Those checks remain on #7145 after a separately authorized merge.

## Recorded evidence

The October 19, 2026 built-preview run passed all four scenarios. Its
[check receipt](evidence/checks.json) records page widths with panels closed,
open, selected and after navigation. Captures are checked-in review evidence,
excluded from both production and fixture build inputs:

- [390 light](evidence/campsites-light-390.png)
- [390 dark](evidence/campsites-dark-390.png)
- [1440 light](evidence/campsites-light-1440.png)
- [1440 dark](evidence/campsites-dark-1440.png)
