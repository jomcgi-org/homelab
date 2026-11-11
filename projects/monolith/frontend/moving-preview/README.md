# Moving page browser checks

This fixture mounts the production root layout, friends layout and moving page.
The root error component is in the module graph because SvelteKit preloads its
legacy and technical-drawing styles on friends routes. The synthetic page store
uses a friends hostname and exercises the root layout's Nav suppression.

The build runs the real server loader with synthetic responses for ready,
forbidden and unavailable states. No server module enters the browser bundle.
The browser checker serves an allowlisted local artifact and synthetic moving
API. Chat uses a local HTTP event stream. Every unexpected request fails the
check. The two production font stylesheets are intercepted without egress;
Schibsted Grotesk comes from the existing vendored preview font, and code text
uses the installed monospace fallback.

## Coverage

| Surface or state                                          | Evidence                                                                                                              |
| --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Forbidden and unavailable, title and description          | AccessPanel tests, composed route DOM tests, both widths and schemes                                                  |
| Ready dashboard, populated and empty collections          | Moving helpers, composed DOM tests, both widths and schemes                                                           |
| Mine/All and dashboard/manage navigation                  | DOM and browser tests preserve query parameters and existing mode/scope semantics                                     |
| Today/Plan/More phone tabs and viewport changes           | DOM listener cleanup, phone screenshots, desktop-to-phone-to-desktop browser resize                                   |
| Task updates                                              | Pending protection, failure rollback, retry and shared task binding in DOM/browser tests                              |
| Management spans, milestones, tasks, roles and collisions | Existing ManagePanel create/edit/delete/undo/error tests, composed binding tests, populated and editing browser views |
| Task, sell, calendar, roles and chat dialogs              | Native focus, keyboard Escape, focus restoration and contained scrolling; backdrop/Close behavior in DOM tests        |
| Chat streaming                                            | Existing component/parser tests plus local HTTP completion, error, history and close-abort browser tests              |
| Crowded timeline and long labels                          | Shared synthetic fixtures, existing packing tests, 34px row and 46px axis-padding assertions                          |

Browser captures cover 390px and 1440px widths in light and dark. The short
390px by 520px chat viewport checks the dialog bounds and composer after resize.
`checks.json` records computed body, page, panel and dialog overflow, dimensions,
requests, abort counts and artifact hashes. Native focus rings are captured
after keyboard input. Playwright traces retain each interaction.

The legacy baseline at `f14f482352ecdb7fb63d188f77bbc6b52f8b77ca` was built with
the same harness and synthetic data. Body overflow is `auto` on both versions,
page overflow stays `visible`, and the desktop agenda retains `contain: size`
and vertical scrolling. The migration deliberately contains timeline overflow
inside `.card-plot`. Native dialogs now use an `overflow: hidden` flex frame with
an independently scrolling body, keeping their header and Close control visible.
Chat retains its inner message scroller and hidden body overflow. The 34px
milestone/bar rows, 38px leg stagger, 46px axis padding and JS packing constants
are unchanged. Phone Plan keeps the existing list presentation of the timeline.

## Run and inspect

From `projects/monolith/frontend`, with workspace dependencies installed:

```sh
pnpm exec vitest run src/routes/friends/moving
MOVING_PREVIEW_SHA="$(git rev-parse HEAD)" pnpm exec vite build --config moving-preview/vite.config.js
node moving-preview/browser-check.mjs "$PLAYWRIGHT_MODULE" \
  moving-fixture-preview /tmp/moving-evidence "$(git rev-parse HEAD)"
```

`PLAYWRIGHT_MODULE` is the absolute path to the pinned Playwright 1.63.0
`driver/package/index.mjs`. Its Chromium and OS libraries must be installed.
The checker rejects a build stamp that differs from the requested commit. For a
separately built legacy checkout, `--baseline` records its original geometry and
screenshots without applying migration assertions. Never stamp changed source
as an unchanged commit.

Publish the evidence directory and tested build with the exact commit SHA when
running this checker in CI. Inspect the PNGs for clipping, text readability and
focus in addition to the assertions. Full-page management captures are tall;
inspect their forms at native resolution. This fixture does not establish
real-host or deployment acceptance. Those checks remain on #7158 after an
authorized merge and publish.
