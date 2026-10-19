# Leaderboard and artifact fixtures

This browser-only fixture mounts the production root layout, public layout,
leaderboard and artifact pages. It also imports the root error component, as
Kit does when preloading its CSS. The report in `test/sheet-fixtures.js` is
invented. `artifact.html` is a harmless sandboxed document served only by the
browser check's local request handler. No backend, live artifact, credentials
or telemetry is used. Loading the fixture without that handler cannot serve
the artifact's raw/version paths.

From `projects/monolith/frontend`, after a frozen dependency installation:

```sh
pnpm exec vite build --config sheet-preview/vite.config.js
python3 sheet-preview/browser_check.py --artifact sheet-fixture-preview --output /tmp/sheet-evidence
```

The check uses the repository's Playwright 1.63.0 and Chromium installation.
`--executable` can select an available local Chromium for advisory checks.
The report records the browser version. Linux CI uses pinned Playwright's
Chromium. The local font is the existing vendored Schibsted Grotesk from
`preview/fonts/`; external font CSS requests are fulfilled locally with empty
CSS. All other non-fixture requests are rejected.

Every capture is 1000px high, at widths 390 and 1440, in both light and dark.
The eight opening screenshots include both pages. Four additional full-page
leaderboard captures show all charts and task rows, and four artifact captures
show the maintenance banner. Geometry assertions check
horizontal page overflow and every iframe edge against the viewport. The
artifact checks also mount the production maintenance banner, confirm that it
makes the document taller than the viewport and verify computed body overflow
stays hidden with no outer-page wheel scrolling. They retain the banner's
existing offset above the full-height iframe. Both page overrides are checked
again with the shared CSS rule loaded last, including the leaderboard trail's
zero bottom margin. Both prefixed artifact paths run the same overflow checks
with and without the banner. The browser also checks sandbox isolation, page
titles, canonical metadata, accessible controls, focus outlines, keyboard
interaction, scheme persistence, selection, sorting and shallow query updates.
The artifact owns its own document scheme; the wrapper does not inject styles
or a theme control into untrusted HTML. DOM tests additionally use fake timers
for version failures, cache busting, route changes and teardown.

Before migration, wrapping the real iframe in the generic `.sheet > .frame`
changed its 390x1000 bounds from `(0,0,390,1000)` to `(16,16,358,1000)`.
At 1440x1000 the result was approximately `(72.8,27.3,1294.4,1000)`.
The artifact therefore uses a page-local zero-padding sheet without a frame,
spine or chrome row. The root layout's nav suppression stays unchanged.

`sheet_fixture_preview_build_test` checks the hand-listed input closure in
Linux CI. The existing `preview/browser_check.py` builds and checks this
separate output after its blog checks. The existing Fixture preview build
workflow uploads the screenshots, traces and `sheets/result.json` in its
exact-head `fixture-preview-evidence-<sha>` artifact. Link that artifact in
the delivery PR. The sheet build is never copied into the Pages publication
artifact, whose iframe-forbidding CSP remains unchanged. Generated outputs
and screenshots must not be committed.

This is repository evidence only. Rollout and real-host acceptance remain
outstanding on #7148 after a separately authorized merge.
