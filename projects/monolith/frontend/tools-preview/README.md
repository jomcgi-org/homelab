# Private tools fixture evidence

These are the real review, chat, notes, Qwen and private Grimoire pages under
the production root/private layouts, persisted scheme setup, local production
font and the root error boundary's legacy CSS import. Server loaders and
SvelteKit navigation are fixture seams. Records, SSE responses and inference
stats are invented. No private services, authentication or production records
are used. Browser paths are resolved against the production `hooks.js`.

```sh
pnpm --dir projects/monolith/frontend exec vitest run --config tools-preview/vitest.config.js
FACTORY_PREVIEW_SHA=$(git rev-parse HEAD) pnpm --dir projects/monolith/frontend exec vite build --config tools-preview/vite.config.js
python3 projects/monolith/frontend/tools-preview/browser_check.py --artifact projects/monolith/frontend/tools-fixture-preview --output /tmp/tools-evidence --expected-sha "$(git rev-parse HEAD)"
```

The browser runner requires Playwright 1.63.0 and its Chromium. It runs one
browser and closes each context before opening the next. Twenty captures cover
each page at 390/1440 in light/dark, plus nested-scroller captures. The result
and Markdown index record the head, viewport, route, scheme, image digest,
document and nested overflow checks, interactions and failures. Errors retain
partial screenshots and memory readings.

`//projects/monolith/frontend:tools_page_dom_test` runs page DOM tests on Linux.
`//projects/monolith/frontend:tools_fixture_preview_build_test` builds the
browser harness. The existing Factory mobile preview workflow runs both the
DOM suite and browser matrix for PR readers. The existing Python entry point invokes
`tools-preview/ci_check.py` serially. Download
`factory-preview-evidence-<head>` and open `tools/MATRIX.md`.
Evidence proves synthetic page behavior only. Live acceptance remains on #7156.
