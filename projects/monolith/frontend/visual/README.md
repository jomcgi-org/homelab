# Public-pages visual regression

A small, self-contained Node tool (its own `package.json`, deliberately NOT part
of the hermetic Bazel app build) that deterministically screenshots every public
monolith SvelteKit page against committed mock data, then pixel-diffs the PR
render against the `origin/main` render. A dedicated BuildBuddy action runs it on
PRs and posts before/after/diff images for the pages that changed. There are no
committed baselines and no reseed step: `main` IS the baseline, rendered on
demand.

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

## Branch-vs-main: there are no committed baselines

The "before" image is `origin/main`'s render, captured on demand in the same CI
run as the "after" (PR) render. Both come from the identical Linux Playwright
exec image, so font hinting and WebGL (SwiftShader) rasterization match by
construction. macOS vs Linux differs at the pixel level, so the smoke-run below
validates only that pages render and the diff math works, never the pixels.

CI renders both sides with an in-place ref-switch in one Bazel output_base
(`git checkout origin/main` between the two `:capture` builds), so the second
build reuses the warm analysis cache instead of paying a cold worktree analysis.
A cheap path gate (`git diff origin/main HEAD -- <frontend subtree, shared CSS,
workspace lock, MODULE.bazel>`) skips both captures entirely when nothing
render-relevant changed; the gate is a SUPERSET of `:capture`'s inputs, so being
too broad only costs a redundant cache-hit build, never a missed regression.

There is nothing to accept and no auto-merge hazard: when a PR that changes a
page merges, `main`'s render moves with it, so the next PR diffs against the new
truth automatically. The action is informational (no `depends_on`), so it never
gates merge.

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
render and the pipeline runs end to end; the resulting pixels are NOT
CI-comparable (see "Branch-vs-main" above). To exercise the diff math locally,
capture two trees into separate dirs and run
`CAPTURE_DIR=out BASELINE_DIR=other-out node diff.mjs`.
