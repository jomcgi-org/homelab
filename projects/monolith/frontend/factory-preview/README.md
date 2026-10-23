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

## Complete sheet layout comparisons

`sheet-vite.config.js` adds a separate, synthetic-only entry for blog index,
TOC, single-column and demo posts, every public factory detail consumer, and
the private updates journal. It mounts the production root and tier layouts,
reset, navigation suppression and chrome. It uses the same local font as the
older harness. The root error boundary's preloaded reset remains represented
by the public stylesheet import; no production loader enters the browser bundle.

```sh
pnpm exec vitest run --config factory-preview/sheet-vitest.config.js
FACTORY_PREVIEW_SHA=$(git rev-parse HEAD) pnpm exec vite build --config factory-preview/sheet-vite.config.js
python factory-preview/sheet_check.py --artifact sheet-fixture-preview --output sheet-evidence --label "$(git rev-parse HEAD)" --acceptance
```

The DOM target generates its invented loader records within its own test run,
so it does not rely on another target's output. The browser suite captures
390px and 1440px light/dark pages, including maintenance notices, long content,
filtered and empty journals. It records computed overflow and sticky positioning
before and after scrolling, exercises detail disclosures, checks persisted scheme
choices opposite the OS, and advances the clock to check polling and cleanup.
All network access other than the local static artifact is denied. The fake
fetch function accepts only known synthetic read endpoints.

Linux CI builds the same head-owned harness against the PR base source using
`SHEET_SOURCE_ROOT`, then compares the base and head screenshots and scroll
contracts with `sheet_check.py --before ... --after ... --output ... --acceptance`.
Visible pixel changes, changed document height, changed scroll ownership and
changed settled keyboard-focus treatment fail
that comparison. Mobile journals also retain eight fresh-context renders: the
unchanged base's fractional sticky/backdrop layer can rasterize text a pixel apart.
The check requires identical geometry in all probes and an exact whole-image
base/head match, with no masked pixels, blur removal or tolerance. The comparison
names any matching probe pair and retains all original images and primary diffs.
Comparator unit tests reject a one-pixel change, including when probes exist.
Both comparison images use the exact viewport width and full document height,
so an overflowing baseline does not silently expand Chromium's raster surface.
Its raw expanded full-page capture is retained as `*-overflow.png`, and geometry
still records those extra columns; the head must have no horizontal page overflow. The
`factory-preview-evidence-<head-sha>` artifact's `sheets/` directory includes both revisions, comparisons,
geometry and interaction screenshots. Review those images before acceptance.
These invented fixtures are not live-host, deployment or authorized-access checks.
