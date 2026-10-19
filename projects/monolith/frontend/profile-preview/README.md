# Home and CV fixture preview

This fixture mounts the production root layout, public layout, home, CV,
engineering, Firecracker and legacy Ember components. It uses the real theme styles and
the vendored Schibsted Grotesk font from `preview/fonts/`. The stats in
`fixtures.js` are invented. No backend, credentials or production data are used.

The small history adapter drives both `$app/state` and `$app/stores` through
the production `reroute` function. It exercises component navigation and CSS
retention. It does not reproduce SvelteKit's server loads, error-node preload
timing or actual-host routing. The composed DOM tests cover private/friends
route suppression; live host acceptance stays on #7016.

From `projects/monolith/frontend` after `pnpm install --frozen-lockfile`:

```sh
pnpm exec vite --config profile-preview/vite.config.js --port 4173
```

In another shell, with Playwright and its Chromium dependencies installed:

```sh
node profile-preview/browser-check.mjs /absolute/path/to/playwright/index.mjs /absolute/path/to/evidence
```

The browser check fixes the clock at October 18, 2026, 12:00 UTC and captures
home and CV at 390 x 1000 and 1440 x 1000 in both light and dark. It saves
full-page and opening screenshots, empty-stats/banner screenshots and a
`checks.json` receipt. Google Fonts requests are fulfilled locally with empty
CSS; the fixture's local font supplies the document face. A system monospace
font is still needed for the sheet's mono labels.

Checks include one chrome/scheme control, headings, the factory CTA, the
homelab fragment, focus visibility, keyboard scheme changes, persisted
scheme after reload, and component navigation through home, CV, engineering,
legacy Ember, legacy Nav and home again. Legacy Ember's computed paint is
compared between a fresh load and navigation from a sheet. Every visited
production surface is checked for horizontal page overflow.

Keep generated output outside the worktree. These captures are repository
evidence only. Actual-host navigation and rollout acceptance remain separate.

## Firecracker evidence

`firecracker-browser-check.mjs` visits `/ember/firecracker` through the same
production layout fixture. Start the fixture above, build the production
frontend, and run its adapter-node server on port 4174 for the separate
no-JavaScript check:

```sh
HOST=127.0.0.1 PORT=4174 node dist/index.js
node profile-preview/firecracker-browser-check.mjs /absolute/path/to/playwright/index.mjs /absolute/path/to/evidence
```

The checker records the source SHA, working-tree state, 390/1440 widths,
both schemes, scroll and replay states, PNG filenames and measured horizontal
overflow in `checks.json`. It exercises all twelve runs and wraparound, slow
mode, reduced motion, keyboard focus, scheme persistence, resize and navigation
through engineering, legacy Ember and legacy Nav. No-JavaScript evidence uses
the production server's HTML, not the client-only fixture. Optional
`CHROMIUM_EXECUTABLE_PATH` selects an already installed browser.

Set `FACTORY_PREVIEW_SHA` to the PR head to require a clean tracked tree at that
exact commit. Preliminary local captures of a dirty tree are marked as such
and cannot settle exact-head acceptance. Keep the receipt beside its PNGs.
Publishing that evidence to the PR is a separate delivery step; running the
checker alone does not upload it. No credentials or production deployment
are involved in capture.
