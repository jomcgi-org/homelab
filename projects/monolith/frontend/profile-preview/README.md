# Profile and Ember component preview

This fixture mounts the production root layout, public layout, home, CV,
engineering, Ember landing and retired Bazel/Postgres fallback components. It
includes the legacy root-error stylesheet and the Ember stylesheet retained by
the separate agents story, plus the vendored Schibsted Grotesk font from
`preview/fonts/`. The stats in `fixtures.js` are invented. No backend,
credentials or production data are used.

The history adapter drives both `$app/state` and `$app/stores` through the
production `reroute` function. It exercises component navigation and retained
CSS. It does not reproduce server loaders, error-node preload timing or
actual-host routing. The Bazel and Postgres captures are explicitly labelled
fallback component fixtures: their real routes return HTTP 308 to
`/ember/firecracker#replay`. `src/routes/public/ember/redirects.test.js` checks
those loaders, including their unchanged handling of query strings.

From `projects/monolith/frontend` after `pnpm install --frozen-lockfile`, build
and run with Playwright and its Chromium dependencies installed:

```sh
pnpm exec vite build --config profile-preview/vite.config.js
node profile-preview/browser-check.mjs /absolute/path/to/playwright/index.mjs /absolute/path/to/evidence
```

The check serves the built fixture on port 4173. For manual development instead,
use `pnpm exec vite --config profile-preview/vite.config.js --port 4173`.

The browser fixes the clock at October 18, 2026, 12:00 UTC and requests reduced
motion. It captures all five components at 390 x 1000 and 1440 x 1000 in light
and dark, with full-page and opening screenshots. The Ember matrix alone has
12 combinations, plus expanded architecture captures. `checks.json` records
the source HEAD, surface, dimensions, scheme, document/body overflow and image
names. Google Fonts requests are fulfilled with empty CSS; the local font
supplies the document face. Mono labels still depend on system fonts.

Checks include one visible trail and scheme control, expected links, keyboard
focus, persisted scheme after reload, zero document overflow, each expanded
Ember section, and navigation through home, CV, engineering, Ember, legacy Nav
and home again. Ember's computed paint is compared between a fresh load and
navigation from a sheet. Existing private/friends suppression and legacy theme
assertions remain in the composed DOM tests.

Set `PROFILE_PREVIEW_SHA` to require the checkout HEAD to match the expected
source revision. No existing Linux workflow invokes this browser checker.
Its integration and artifact publication are outstanding repository acceptance
on #7150; unrelated green preview jobs do not establish this coverage.

Keep generated output outside the worktree. These captures are repository
evidence only. Actual-host navigation and rollout acceptance remain on #7150
for Ember and #7016 for the wider migration.

## Ships

`/app/ships` mounts the production route, ShipsApp, ShipsMap and both layouts,
including the root error node's retained legacy CSS. The preview navigation
adapter updates the URL without remounting the map. Its browser environment
stub is explicitly true, so this exercises the client guard as well as chrome.
`test/ships-fixtures.js` supplies invented AIS positions, heat cells and tracks.
The renderer, provider tiles, sprites, glyphs and attribution are real.

```sh
node profile-preview/ships-browser-check.mjs /absolute/path/to/playwright/index.mjs /absolute/path/to/evidence
```

The check fixes the clock at October 19, 2026, 12:00 UTC. It captures vessels,
a selected vessel and heat at 390 and 1440 pixels in both schemes. Assertions
require loaded map content, no overflow, one chrome/control, keyboard scheme
changes without camera/filter/track loss, URL restoration, pan, zoom and
attribution. Provider bytes can be relayed through the guest's curl egress;
they are not replaced with a mock map. This requires public provider access
and does not test real-host data or post-merge rollout. Selected reviewed
captures are archived under `projects/monolith/dev/ships/evidence/`, following
the existing `dev/grimoire/evidence/` convention. Other generated output stays
outside tracked source. The PR links the archived files directly.
