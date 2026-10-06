# Synthetic theme preview

This fixture renders explicit light and dark technical-drawing boundaries and a
dark inset inside the light boundary. It has text samples on both surfaces,
keyboard controls, labelled status glyphs, and five chart series with labels and
distinct marker shapes. Every colour reads a `--ds-*` role. It uses synthetic
constants only and lives outside the production source closures.

`DataDisplayFixture.svelte` imports every shared data-display component through
the package subpath. Each boundary includes dense and sparse definition lists,
all status meanings, zero/negative/fractional/large/missing/non-finite metrics,
long labels and units, a captioned fallback table and every content state.
The same composition supplies SSR and hydration evidence for every primitive.

The Vite build server-renders `ThemeFixture.svelte` through `svelte/server` in a
Node module graph, then the client hydrates that markup. The Vitest checks load
the server entry without browser globals and assert that hydration retains
nodes, text and attributes without warnings. A button checks attached handlers.

From the frontend directory:

```sh
pnpm exec vitest run --config theme-preview/vitest.config.js
FACTORY_PREVIEW_SHA=$(git rev-parse HEAD) pnpm exec vite build --config theme-preview/vite.config.js
python theme-preview/browser_check.py --artifact theme-fixture-preview --output /tmp/theme-evidence --expected-sha "$(git rev-parse HEAD)"
```

The browser check requires the same pinned Playwright Chromium runner as the
Factory mobile preview workflow. It checks both browser schemes at 320x740 and
1440x1000 with 100% and actual 200% text sizes, records computed contrast,
checks clipping and keyboard focus rings, and writes JSON and screenshots.
It checks text rectangles for overlap, rejects ellipsis and text below 16px,
opens every native disclosure with the keyboard, and repeats layout and contrast
checks on expanded exact values and tables. Every disclosure must have a 44px
minimum target and a role-coloured focus outline. It verifies visual reading
order in metrics, definition lists, legend and table, exact-value accessibility,
and stable legend role colours/shapes in each boundary. Eight full-page
screenshots record both schemes at both widths and text sizes with disclosures
expanded. `result.json` contains collapsed and expanded contrast/layout checks,
focus records and per-boundary data coverage. `tested-files.json` hashes the
unchanged build artifact. Browser failure is a failing check, never a skip.
The workflow uploads `theme-preview-evidence-<head sha>` and
`theme-preview-<head sha>`. No page selects this theme automatically.

## Composition gallery

`gallery.html` is a second entry in the same build. Its sources live under
`gallery/`. `GalleryFixture.svelte` composes the exported controls, navigation
and data-display primitives into a dashboard and a document, with focused
examples for each group. The light and dark sheets surround a contract-only
region and a nested dark boundary followed by a light sibling. All invented
labels, dates, references, values and initial interaction inputs live in
`gallery/fixtures.js`. The fixed chart is caller-owned SVG with a labelled
legend and an exact-value table in the ChartFrame fallback disclosure.

The package's `DATA_DISPLAY_FIXTURES` supply measurement edges. The gallery
pins the supported status kinds, content states and series IDs; its contract
test checks complete coverage, the meaning/role/marker contract and rejected
inputs. A contract change requires an explicit gallery update. Existing
Factory fixtures and loader validation stay unchanged.

The commands above run gallery contract, Node SSR, hydration, import-resolution
and entry-routing tests, then build both `index.html` and `gallery.html` in
`theme-fixture-preview/`. The original page keeps its own server renderer.
Both client entries and their Node renderers reject production frontend and
`$app/` modules. Resolution tests also render the gallery through the real
frontend `vite.config.js`, without overriding its SSR conditions. The fixture
uses the existing local font and contains no animation, time or live fetch.

To inspect locally, serve the built directory, for example:

```sh
python3 -m http.server 8080 --directory theme-fixture-preview
```

Open `/gallery.html`, compare the boundaries and expand chart tables and exact
values. Edit `gallery/fixtures.js` for new synthetic examples, update the
contract assertions only after checking the package, and rerun the commands
above. Keep all added files in the explicit `theme_fixture_src` BUILD list.
The gallery remains outside the production source closures.

The existing `Factory mobile preview` browser job already watches this
directory. Its theme Vitest step discovers `theme-preview/**/*.test.js`, its
Vite step builds both entries, and its artifact upload includes both pages
and `build.json` with the exact head SHA. `browser_check.py` retains all eight
original theme/data-display cases and then imports `gallery_check.py`. The
existing step, "Check theme contrast, keyboard focus, and real text resize",
hosts both matrices. No workflow edit is needed: the push credential lacks
workflow scope, and reusing the harness follows the #6875 data-display precedent.

The gallery matrix has 24 cases: dashboard and document, 320px, 390px and
1440px, both browser schemes, and 100%/200% text. Every page contains explicit
light/dark boundaries, the nested dark inset and the unmarked contract region.
Text-only resizing doubles the measured computed font sizes and numeric line
heights through `resize_text`; it uses no zoom, transform or device scale.
The checker compares text rectangles for clipping and intersections, verifies
DOM/visual reading order, measures 44x44px targets, traverses focus with Tab and
Shift+Tab, and measures rendered text, control border, glyph, marker and focus
contrast. It tests labelled fields, error associations, disabled controls,
arrow-key tabs, repeated disclosures and form entry/reset/submission.

Captures wait for the bundled Schibsted Grotesk and stable layout. External
requests, including fonts, fail. Date is frozen to the fixture date. Reduced
motion captures follow interaction checks in both reduce and no-preference;
active animations and non-zero transition/animation durations fail. Each case
writes a full-page image containing both compositions and explicit schemes,
plus composition, chart, tabs, open disclosure and error-field images in each
explicit boundary. One phone case is captured twice and the digests must match.

Run the commands at the top of this README with Playwright 1.63.0 and its
Chromium installed. Fail-closed unit tests run without Chromium:

```sh
python3 theme-preview/gallery_check_test.py
python3 -c 'import sys; sys.path.insert(0,"theme-preview"); import gallery_check as g; from pathlib import Path; g.preflight(Path("/tmp/empty-gallery-artifact"))'
python3 -c 'import sys; sys.path.insert(0,"theme-preview"); import gallery_check as g; from pathlib import Path; g.validate({"gallery_cases":[],"screenshots":{}},Path("/tmp/empty-gallery-evidence"))'
```

The last two commands must exit non-zero with "gallery page missing from
artifact" and "zero gallery cases ran". Missing Playwright/Chromium or a launch
failure is also fatal. Missing/empty screenshots, incomplete case lists and
artifact SHA mismatches fail. The built `build.json` commit must equal the
requested head SHA, which is recorded in `result.json` alongside each gallery
case, failure messages and the screenshot SHA-256 manifest. Failed cases retain
a Playwright trace and a full-page failure capture when the browser permits it.

Download `theme-preview-<head sha>` and `theme-preview-evidence-<head sha>`
from the exact-head Factory mobile preview run. Check `result.json` has eight
theme cases and 24 passing `gallery_cases`, then inspect the 200pct full-page
and selected images under `gallery/`, in both schemes at all three widths.
Open a failure trace with `playwright show-trace gallery/<case>-failure.zip`.
CI is the Linux authority; Vitest alone does not establish visual acceptance.
Update fixtures or layout only after reading failed measurements and inspecting
the images, then rebuild at the new head and rerun the same command. Never
replace expected screenshots or relax thresholds to conceal a failure.
