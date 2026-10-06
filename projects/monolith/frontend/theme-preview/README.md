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
and `build.json` with the exact head SHA. The unchanged theme browser checker
visits only `index.html`; its tested-file digest manifest also records the
gallery bytes. Download `theme-preview-<head sha>` to inspect the gallery and
`theme-preview-evidence-<head sha>` for the existing eight-case theme evidence.
No workflow, publisher or public route is added.

Gallery-specific Chromium acceptance remains the next node: 320px, 390px and
1440px, both schemes, actual 200% text, keyboard and repeated interactions,
contrast, targets, clipping, isolation, screenshots and visual inspection.
Happy-dom checks do not claim that acceptance. This node preserves the existing
theme browser checks with the extra built page present.
