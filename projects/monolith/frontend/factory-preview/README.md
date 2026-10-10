# Factory fixture QA

This bounded harness imports the production Overview, Activity, and Context
pages, SchemeToggle, Trail, shared tokens, and styles. It cannot fetch a live
origin. All task titles, dates, activity, goals, facts, and identifiers are
invented in `fixtures.js`.

Run from `projects/monolith/frontend`:

```sh
pnpm exec vitest run --config factory-preview/vitest.config.js
FACTORY_PREVIEW_SHA=$(git rev-parse HEAD) pnpm exec vite build --config factory-preview/vite.config.js
```

The contract test calls the three server loaders at this exact commit with
synthetic endpoint responses. It generates ignored `.generated/pages.json`
consumed by the browser build. Unknown loader endpoints fail the test. Model,
chart, activity, and search-index unit tests run in the same command.

The `Factory mobile preview` GitHub Actions job checks the built artifact in
actual Chromium. The matrix covers widths 320, 360, 390, 430, and 1440; light
and dark; live, empty, and unavailable states; context intro, chapter, and
search views; and 200% text. Targeted live and error Overview cases also check
the 901px and 1024px desktop transition in both schemes. Text resizing doubles measured font sizes and
line heights, with no CSS transform or zoom. Layout assertions cover page and
factory-container overflow, wrapped readable mobile titles, visible task
phase/time, chart/legend visibility, 44px navigation/pager targets, reduced
motion, and keyboard controls. The browser checks navigation, Back, filters,
pagination, expandable records, and search suggestions.

CI records its exact head SHA in `build.json` and rejects a mismatch. Evidence
contains a result for every case, full-page screenshots, selected opening
screenshots, failure traces, and SHA-256 digests of every tested output file.
The output is tested beneath a nested URL with relative assets. Out-of-scope
links stay in the fixture; no production requests or credentials are allowed.

This tests real route components and synthetic loader data, not SvelteKit
server routing, live API availability, or real-time revalidation. A passing
layout check does not replace visual review of the phone and desktop images.
