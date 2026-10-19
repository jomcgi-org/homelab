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

### Prepare an Ember capture handoff

`browser_handoff_prepare.py` prepares the paired `output/sheets` bundle emitted
by the corrected `browser_check.py --capture-only` / `sheet_ci_check.py` harness.
It requires that reviewed harness, including `sheet_check.py`, to be present
beside the helper. The original generic browser-check output is not a paired
sheet bundle. This remains a dependency on the capture stack until integrated.

After capture, run the helper with the exact captured source revisions and the
actual input files used for that run. For a multi-file fixture, supply its exact
archive, not an invented digest. The browser input is the actual Chromium
executable, not its path string. All four inputs are copied into the inventory.

```sh
python projects/monolith/frontend/factory-preview/browser_handoff_prepare.py \
  --checkout /workspace/src --evidence-root output/sheets \
  --repo jomcgi-org/homelab \
  --base-sha "$BASE_SHA" --head-sha "$HEAD_SHA" --target-sha "$TARGET_SHA" \
  --harness-sha "$HARNESS_SHA" --browser-version "$CHROMIUM_VERSION" \
  --fixture "$FIXTURE_FILE" --checklist "$CHECKLIST_FILE" \
  --dependency-lock pnpm-lock.yaml --browser-binary "$CHROMIUM_EXECUTABLE" \
  --output browser-handoff.json
```

The helper invokes the existing mandatory checker and full-height bundle
validation, preserves and checks the harness-created `provenance.json` against
the supplied revisions, writes `inputs.json` and `manifest.json`, and emits
only a capture declaration. It does not review content, assert visual acceptance,
or claim an Ember producer/export identity. The server binds producer identity
from its completion receipt before canonical handoff validation. Stale metadata,
missing dependencies (including Pillow), missing/clipped captures and provenance
mismatches fail without emitting a handoff. Existing captures survive a failed
preparation; files created by that failed preparation are removed. The output
handoff must be outside the evidence directory to avoid a self-referential hash.
After these public checks, the browser entry point runs the isolated
`private-factory-preview/ci_check.py`. That fixture builds the three private
factory routes separately, retaining this public build's private-module guard.
The existing evidence upload includes its `private/MATRIX.md` screenshots and
`private/results.json` provenance and overflow/action checks. See that fixture's
README for its scope; a public preview pass alone does not cover private pages.
