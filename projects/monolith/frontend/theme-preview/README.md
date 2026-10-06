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
