# Public-pages visual regression

A small, self-contained Node tool (its own `package.json`, deliberately NOT part
of the hermetic Bazel app build) that deterministically screenshots every public
monolith SvelteKit page against committed mock data, then pixel-diffs the result
against committed baselines. A dedicated BuildBuddy action runs it on PRs that
touch the frontend and posts before/after/diff images for the pages that changed.

This mirrors how `bazel/helm/ci-diff-manifests.sh` is a standalone PR-comment
script, not a `bazel test`. Do not build an apko image for it: the CI runner is
an external, pinned Playwright container, and the "apko / dual-arch / uid 65532"
rules apply only to shipped runtime images.

## The `API_BASE` mock seam

Every public `+page.server.js` `load()` calls `fetch(\`${API_BASE}/api/...\`)`.
We boot the real adapter-node app (the Bazel `:build_public`output) with`API_BASE` pointed at a local mock server (`mock-server.mjs`) that serves small,
committed JSON fixtures from `fixtures/api/`. Because the real app runs, its own
same-origin `+server.js`proxy routes (e.g.`/app/ships/heat`,
`/app/notes/stats`) execute inside the app and call `API_BASE`themselves, so the
mock only ever needs to serve the raw`/api/...` backend paths, never the proxy
site paths.

Fixtures are past-dated and deterministic. The browser clock is frozen per page
in the capture step so "X ago" copy and `setInterval` marquees are stable.

## Baselines are CI-only

Baseline PNGs are valid ONLY when generated in the Linux Playwright container,
because macOS vs Linux font hinting and WebGL (SwiftShader) rasterization differ
at the pixel level. Never commit baselines captured on a workstation. The mock
server, capture, and diff logic CAN and SHOULD be smoke-run locally (pages
render, diff math works); only the committed pixel baselines must come from CI.

## Reseeding baselines: do not auto-merge the sentinel PR

To accept a render change, commit an empty `.reseed-baselines` sentinel. The
PR-triggered visual action then reseeds the baselines, commits them back to the
PR branch as `visual-baseline-bot`, and deletes the sentinel (one-shot). That
reseed runs ONLY in `pull_request` context (there is no `push: main` trigger),
and the action is informational (no `depends_on`), so it does not gate merge.

Consequence: never enable auto-merge on a reseed PR. Auto-merge can pass the
required checks and merge the raw sentinel onto main in a couple of minutes,
before the slower visual action (npm install + chromium + screenshots) pushes
the baseline commit. A sentinel that reaches main is then stuck (nothing
consumes it on `push: main`) and the next unrelated PR silently reseeds. Instead
wait for the `visual-baseline-bot` commit to land on the PR branch, confirm the
baselines updated, then merge.

## Basemap interception (Option A: flat backdrop)

Map pages render maplibre against `tiles.openfreemap.org`, a third-party host
whose tiles are non-deterministic and slow. The capture step intercepts every
`tiles.openfreemap.org` request and fulfills the style request with a committed
flat background style (`fixtures/basemap/blank-style.json`, a single solid
`background` layer, no vector source), and 404s any other sub-request so nothing
hangs. maplibre then makes zero tile/glyph/sprite calls and paints a flat
backdrop under our real overlay data, so the map pages diff deterministically on
the overlay, not the basemap.

## Smoke-running locally

```bash
cd projects/monolith/frontend/visual
npm install
npx playwright install chromium

# Build the adapter-node public app (routes through remote BuildBuddy; may be slow).
bazel build //projects/monolith/frontend:build_public

# Boot the mock + app and capture screenshots into out/.
APP_ENTRY=$(bazel info bazel-bin)/projects/monolith/frontend/build_public/index.js \
  node capture.mjs
```

`out/` and `node_modules/` are git-ignored. On macOS this validates that pages
render and the pipeline runs end to end; the resulting pixels are NOT a valid
baseline (see "Baselines are CI-only" above).
