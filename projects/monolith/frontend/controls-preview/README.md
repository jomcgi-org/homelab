# Shared controls fixture

Synthetic SSR and hydration evidence for #6874. `ControlsFixture.svelte` imports
all six primitives through `@homelab/design-system/components`. It renders light,
dark, a dark inset within light followed by a light sibling, and a contract-only
sample. It has no production
page, route, store, API, or private import. Its interaction state is local.

From `projects/monolith/frontend`:

```sh
pnpm exec vitest run --config controls-preview/vitest.config.js
FACTORY_PREVIEW_SHA=$(git rev-parse HEAD) pnpm exec vite build --config controls-preview/vite.config.js
python controls-preview/browser_check.py --artifact controls-fixture-preview \
  --output /tmp/controls-preview-evidence --expected-sha "$(git rev-parse HEAD)"
```

`ssr.test.js` imports and renders with browser globals undefined.
`hydration.test.js` hydrates actual server HTML with `recover: false`, retains
element identity, text and every attribute, and checks zero console warnings or
errors before exercising handlers. Native controls use an uninitialized input
and a select: Svelte intentionally normalizes native input `value`/`checked`
attributes during hydration, separately from field association IDs.
`resolution.test.js` checks barrel/direct component exports and that each resolves
the same physical Svelte client runtime as the frontend fixture.

Happy-dom does not execute native summary keyboard toggling or browser tab order,
and cannot prove layout, contrast, focus appearance, or hit-target dimensions.
`browser_check.py` requires Playwright 1.63.0 and Chromium. Missing Chromium is a
failure, never a skip. It reuses `factory-preview/` artifact hashing and
`theme-preview/` colour compositing, clipping checks and real text-only resize.
Its eight cases cross 320x740 and 1440x1000, browser light/dark scheme, and 100/200%
computed font size. Each case checks every themed boundary, then repeats the
complete Tab/Shift+Tab and activation sequences after a reload and compares their
results. The JSON records accessible names/descriptions and associations,
implicit submitter, disabled activation, disclosure indicator changes, automatic
tab activation, 44px targets, computed contrast and focus, state cues, clipping,
unrelated text/control overlap, nested roles and reduced motion. Layout and text
contrast are also checked with disclosures expanded. Button boundaries are
measured against the adjacent exterior, matching the theme fixture's rule.

The Vite build server-renders `index.html`. Both compilation graphs reject
production frontend and `$app/` modules. `main.js` hydrates with `recover: false`
and rejects node, text or attribute changes. The browser separately loads the
served artifact with scripts disabled before checking hydrated interactions and
zero console warnings/errors/page errors. `build.json` must match the requested
commit. No external requests are allowed by the hydrated browser check.

`result.json` and `tested-files.json` accompany 40 screenshots: one full page and
four boundary crops per case, named `<viewport>-<scheme>-<text-size>[-<boundary>].png`.
The Factory mobile preview workflow uploads both evidence and tested bytes under
exact-head artifact names. No binary evidence is committed. The fixture stays
outside `:src` and `:src_public`; its files and build-test are enumerated in the
frontend BUILD. Do not publish the preview or migrate a production page.

Browser selectors: `data-sample="light|dark|nested|sibling|contract"`,
`data-action="default|submit|disabled|named|field|header|disclosure|selection|external-submit"`,
and `data-state="submissions|submitter|disclosure|selection"`. The four themed samples
share presentational state so binding changes can be observed across boundaries.
Optional fixture props `buttonType`, `orientation`, `headingLevel`, `initialSelected` and
`tabs` cover vertical tabs, single tabs and selection fallback in Node tests.
