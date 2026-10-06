# Synthetic theme preview

This fixture renders explicit light and dark technical-drawing boundaries and a
dark inset inside the light boundary. It has text samples on both surfaces,
keyboard controls, labelled status glyphs, and five chart series with labels and
distinct marker shapes. Every colour reads a `--ds-*` role. It uses synthetic
constants only and lives outside the production source closures.

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
The workflow uploads `theme-preview-evidence-<head sha>` and
`theme-preview-<head sha>`. No page selects this theme automatically.
