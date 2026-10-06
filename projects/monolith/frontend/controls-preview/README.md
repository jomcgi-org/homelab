# Shared controls fixture

Synthetic SSR and hydration evidence for #6874. `ControlsFixture.svelte` imports
all six primitives through `@homelab/design-system/components`. It renders light,
dark, a dark inset within light, and a contract-only sample. It has no production
page, route, store, API, or private import. Its interaction state is local.

From `projects/monolith/frontend`:

```sh
pnpm exec vitest run --config controls-preview/vitest.config.js
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
Those checks require Chromium. The next delivery node adds the Vite entry/build,
`browser_check.py`, workflow steps and screenshots here, reusing the existing
`factory-preview/` browser helpers and `theme-preview/` SSR build pattern. Keep
this fixture outside the frontend `:src` and `:src_public` targets. Add each new
fixture file to `:controls_fixture_src` and the preview/build-test targets when
adding that build. Do not publish the preview or migrate a production page.

Browser selectors: `data-sample="light|dark|nested|contract"`,
`data-action="default|submit|disabled|named|field|header|disclosure|selection|external-submit"`,
and `data-state="submissions|disclosure|selection"`. The three themed samples
share presentational state so binding changes can be observed across boundaries.
Optional fixture props `orientation`, `headingLevel`, `initialSelected` and
`tabs` cover vertical tabs, single tabs and selection fallback in Node tests.
