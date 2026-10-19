# Grimoire sheet fixture

This harness mounts the eight production rendered pages and their root/public
layouts. The standalone share deliberately skips the public and Grimoire
layouts, matching `+page@.svelte`. The landing retains its already-approved
baked excerpt; every API response and identifier in the fixture is invented.
No backend, session credential, or production endpoint is used.

`fixtures.js` replaces fetch only inside this harness. Unexpected calls fail
closed. The simulated challenge widget calls the production TurnstileGate and
Grimoire admission helper. It cannot prove real Turnstile admission. Server-load
and redirect tests run separately against the production loaders.

From the frontend directory with frozen workspace dependencies installed:

```sh
pnpm exec vitest run --config grimoire-preview/vitest.config.js
pnpm exec vite build --config grimoire-preview/vite.config.js
```

The DOM and build targets are in frontend/BUILD. The browser runner needs
Playwright 1.63.0 and Chromium system libraries. Run it from the checkout root:

```sh
python projects/monolith/frontend/grimoire-preview/browser_check.py \
  --artifact projects/monolith/frontend/grimoire-fixture-preview \
  --output /absolute/path/outside/the/worktree \
  --expected-sha <exact-source-head>
```

Its `checks.json` records the exact checkout head, route, width, scheme,
assertions, numeric document overflow and PNG filename for every capture.
Browser failure preserves the screenshots and partial manifest locally.
The existing BuildBuddy Grimoire browser runner builds and captures this
fixture after its friends scenarios, reusing its pinned Playwright environment.
The `grimoire/public-sheet` artifacts contain this surface's PNGs and manifest.
The unrelated Actions preview workflows are not Grimoire screenshot evidence.

The matrix is landing, library, world, entity, adventure, reader, chat and
standalone share, each at 390 and 1440 pixels in light and dark. Additional
captures cover the scroll story with motion enabled, chapters, locked reader,
source drawer, fork challenge, empty share and empty/error library. Reduced
motion is exercised by the full baseline matrix. Intermediate-width reader
reflow, generated-cover contrast and navigation from Engineering have separate
checks. Keyboard focus, scheme switching and persistence are asserted for each baseline page. PNGs and build
output are generated outside tracked source and must not be committed.

This is repository evidence. It does not establish production routing, access,
asset delivery, actual-host navigation, deployment or real admission. Those
checks remain on #7149 after a separately authorized merge and rollout.
