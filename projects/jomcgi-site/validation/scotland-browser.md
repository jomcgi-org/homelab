# Scotland and drafts browser verification

Local verification on 2026-11-17 for HOME-207, PR #8174, using the build from
`e5dec2161bd628886655e717d8387df0648e308d`. The validation update changes no
runtime files. Deployment verification on `next.jomcgi.dev` remains pending.

Chromium 142.0.7444.59 loaded the emitted HTML, CSS and JavaScript through a
loopback HTTP server. The server mapped extensionless URLs to their HTML files
and sent the built `_headers` Content-Security-Policy. Playwright 1.65.1
controlled the browser. Axe 4.14.0 ran against the complete document with all
default rules enabled, including `color-contrast`.

Each page passed four cases: light and dark modes at 1280 × 900 and 390 × 900.
Every case used a fresh browser context.

| Page | Initial JS, gzip | Heading retained after hydration | Browser or console errors | Axe violations | Inline styles after hydration |
| --- | --- | --- | --- | --- | --- |
| `/scotland` | 106.9 KiB | 4/4 | 0 | 0 | 0 |
| `/scotland/privacy` | 106.4 KiB | 4/4 | 0 | 0 | 0 |
| `/scotland/terms` | 106.4 KiB | 4/4 | 0 | 0 | 0 |
| `/scotland/support` | 106.4 KiB | 4/4 | 0 | 0 | 0 |
| `/slop` | 102.4 KiB | 4/4 | 0 | 0 | 0 |

The `color-contrast` rule appeared in axe's passing results in all 20 cases.
The first section link on each Scotland page also changed the URL fragment
to an existing section. Slop has no section index.

## Hydration procedure

The browser initially held requests for `/assets/*.js`. It loaded the HTML
and styles, then retained a handle to the visible `h1`. After releasing the
scripts, it waited for `html[data-astryx-theme="neutral"]`, which the Theme
component sets in its client layout effect. It then checked that the saved
node was still `document.querySelector("h1")` and collected browser errors.
All 20 cases preserved that node without a hydration error.

The pages contain navigation links, text and code samples. They have no
page-specific event handlers, widgets or large datasets to load on intent.
Their entry scripts call [`mount`](../web/src/mount.tsx), which hydrates the
same page tree used by the build. The compiled scripts ran in this check;
the theme effect and preserved heading establish that hydration completed.

## Accessibility procedure

After hydration, the browser counted `style, [style]` elements and loaded the
installed `axe-core/axe.min.js` from the loopback origin. It ran
`await axe.run(document)` and required both an empty `violations` array and
a passing `color-contrast` result. No axe rule was disabled. The page kept
its built stylesheets throughout the check.

The source palette checks are separate from these browser measurements.
[`contrast.test.tsx`](../web/src/contrast.test.tsx) checks colour declarations
outside `palette.css`, inline styles and token contrast in both modes.
[`a11y.test.tsx`](../web/src/a11y.test.tsx) checks every registered page and its
entry imports. Both run in the existing `web:test` target.

## Automated checks and limits

From `web/`, `pnpm test` runs the source hydration, palette and axe checks.
`pnpm build && pnpm budget` measures each entry against the 150 KiB limit.
`node scotland-built.test.mjs dist` checks the emitted pages, assets and
budgets; its registered CI target is `web:scotland_built_test`.
These remain the automated regression checks. The table above records a
one-time browser measurement of the named build.

The initial browser launch used a glibc binary and failed with `ENOENT` in
the Alpine sandbox. The native Alpine Chromium launch also needed its
`usr/lib/pulseaudio` directory on the library path. After those environment
fixes, the complete browser run passed. Neither fix changed the site.

The local server does not verify Cloudflare routing, deployed headers or
production promotion. Remote PR CI and the live preview check remain
unconfirmed by this report.
