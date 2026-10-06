# Design system

This directory contains the experimental shared `--ds-*` token contract from
ADR platform/013. The contract remains wired for compatibility, but #4449
superseded the broader migration and primitive programme. Current frontend work
is not required to adopt it.

This directory is the package. It is not the design documentation. For how
the three themes look, why they are kept apart, the resolved token-collision
history, and the rules a new surface has to respect, read **`.impeccable.md`**
at the repo root. For why a contract layer exists at all, and why it lives
here rather than inside the monolith frontend, read ADR platform/013.

## What is here

```
projects/design-system/
├── package.json                 @homelab/design-system, CSS exports
├── BUILD                        js_library linked via npm_link_all_packages (hand-maintained)
├── README.md                    role guide and tested contrast table
└── tokens/
    ├── contract.css             unchanged --ds-* defaults at :root
    └── technical-drawing.css    explicit, opt-in light/dark boundaries
```

`contract.css` defines the token **roles** (surface, ink, line, accent,
shadow, border weight, type stacks, spacing, radius, status) with the public
neobrutalist values as the baseline. It is a pnpm workspace package
(`pnpm-workspace.yaml`) consumed by `projects/monolith/frontend` as
`@homelab/design-system`, and `src/routes/+layout.svelte` imports it on every
route.

`BUILD` is `# gazelle:ignore` on purpose: the JS extension wants to add an
`npm_package` target, but consumers depend on the `npm_link_all_packages`
link, so the generated target is dead weight that shows up as permanent
`ci regen` drift.

## Current state, honestly

The contract is **wired but not consumed**. As of this README:

- The package is linked and the stylesheet loads on every route, but no
  production Svelte or CSS file in the frontend reads a `var(--ds-*)` token, and none of
  the three themes overrides a `--ds-*` token inside its scope class. The new
  technical-drawing export is opt-in and not yet consumed by any page.
- The five conflicting public tokens are scoped to
  `body:has(.public-theme)`, while the shared defaults remain at `:root`.
  The public layout and root error boundary mount that marker, so the
  brutalist palette follows the rendered surface rather than stylesheet
  import order.
- There is no primitive layer, no `jomcgi.dev/design` gallery, and no
  Storybook, and #4449 does not require any of them.

The live styling rules remain the three per-theme stylesheets named in
`.impeccable.md`, and that file is the document to follow when touching any
of them. The broader contract migration and primitive programme are no longer
outstanding work. This preserves the package history without turning it into
an adoption requirement.

## Opt-in technical drawing

#6822 and #6873 approve a foundation for shared components. Existing Factory,
blog, dashboard, Ember and Grimoire styles remain separate. `contract.css`, its
default values, its `.` and `./tokens/contract.css` exports, and existing
consumers are unchanged. The new `./tokens/technical-drawing.css` export has
no global selector or automatic scheme selection. Importing it alone does not
change an unmarked surface.

```svelte
<script>
  import "@homelab/design-system/tokens/contract.css";
  import "@homelab/design-system/tokens/technical-drawing.css";
</script>

<section data-ds-theme="technical-drawing-light">
  <p>Sheet content</p>
  <section data-ds-theme="technical-drawing-dark">
    <p>Dark inset</p>
  </section>
</section>

<style>
  section {
    font-family: var(--ds-font-body);
    padding: var(--ds-space-md);
    border: var(--ds-border-weight) solid var(--ds-line-strong);
  }
</style>
```

Each boundary declares all 25 existing contract roles and eight theme-only
roles. Its descendants inherit the nearest boundary, including spacing and type
stacks. A light boundary inside dark inside light owns the light values again;
a sibling after the outer boundary reads the contract defaults. Theme-only roles
are empty outside a boundary. Stylesheet import order does not select a scheme.
The boundary selector has higher specificity than the contract's `:root`, so
an explicit boundary on `<html>` also keeps its roles when the contract loads last.

The boundary sets only custom properties plus its own `color-scheme`, ink and
surface background. Components choose their layout, typography and focus
styles. No font is loaded by this export; Schibsted Grotesk falls back to the
listed system sans faces when it is unavailable.

Selection is the literal `data-ds-theme` value. There is no `system` mode,
media query or browser-global read at import time. Render the same attribute on
the server and client. Import safety is tested in Node with browser globals
undefined. The isolated `projects/monolith/frontend/theme-preview/` fixture
server-renders both schemes and a dark-in-light boundary, then hydrates the same
markup. Its tests retain DOM nodes, text and attributes without hydration
warnings. The browser lane checks text resize, keyboard focus, clipping and
computed contrast. No production page imports the opt-in export.

### Roles

| Roles | Meaning |
| --- | --- |
| `--ds-surface`, `--ds-surface-raised` | White sheet / neutral inset in light; neutral-dark sheet / inset in dark |
| `--ds-ink`, `--ds-ink-muted`, `--ds-ink-faint` | Primary, secondary and faint meaningful text, each at least 4.5:1 on both surfaces |
| `--ds-accent`, `--ds-on-accent` | Solid accent fill and text on that fill |
| `--ds-accent-ink` | Links and interactive text on either surface |
| `--ds-line` | Decorative hairline divider only; not a control boundary or meaningful graphic |
| `--ds-line-strong` | Control boundaries and meaningful structural rules, at least 3:1 on both surfaces |
| `--ds-focus`, `--ds-focus-width` | Focus ring colour on either surface and 2px width; components must apply a visible outline |
| `--ds-ok`, `--ds-warn`, `--ds-err` | Success, warning and error text or graphics; pair with explicit labels or distinct shapes |
| `--ds-series-1` through `--ds-series-5` | Stable order: GPU, host RAM, page cache, NVMe, hot expert set; use keyed labels or line/marker shapes too |
| `--ds-shadow`, `--ds-shadow-raised` | `none` |
| `--ds-border-weight`, `--ds-radius` | 1px rules and square corners (`0`) |
| `--ds-font-display`, `--ds-font-body` | Schibsted Grotesk, Avenir Next, Segoe UI, system-ui, sans-serif |
| `--ds-font-mono` | ui-monospace, SF Mono, Cascadia Mono, monospace |
| `--ds-space-xs`, `--ds-space-sm`, `--ds-space-md`, `--ds-space-lg`, `--ds-space-xl`, `--ds-space-2xl` | Same 8, 12, 16, 24, 40, 60px spacing as the contract, redeclared at each boundary |

The figure series use the existing technical-drawing tones in both schemes,
with the same memory-tier meaning. They are measured as graphics, not guaranteed
as small text on raised surfaces. Status roles are text-capable. The known
ornament colours `#97917f` and `#6f6d65` are not exported as text roles. Faint
ink stays `#6b6658` in light and is lifted to `#9b988e` in dark. There is no
ornament role, glow, shadow, gradient, ambient dawn/dusk tint or serif display.

### Measured contrast

WCAG 2.x relative luminance from opaque sRGB `#rrggbb` values. The Node helper in
`projects/monolith/frontend/src/lib/design-system/technical-drawing.test-helper.js`
computes these rows; `technical-drawing.test.js` requires exact agreement to two
decimal places. Text and status require 4.5:1. Focus, strong rules and series
require 3:1. The on-accent row measures against the accent fill in each scheme,
so its value is repeated across that scheme's surface columns.

<!-- contrast:start -->
| Role | Light sheet `#ffffff` | Light raised `#f5f6f8` | Dark sheet `#181a20` | Dark raised `#1c1e26` |
| --- | --- | --- | --- | --- |
| `--ds-ink` | 15.68 | 14.50 | 13.81 | 13.20 |
| `--ds-ink-muted` | 6.56 | 6.07 | 6.80 | 6.50 |
| `--ds-ink-faint` | 5.73 | 5.30 | 6.03 | 5.76 |
| `--ds-accent-ink` | 7.84 | 7.25 | 7.66 | 7.32 |
| `--ds-ok` | 5.07 | 4.69 | 7.88 | 7.53 |
| `--ds-warn` | 6.10 | 5.64 | 7.31 | 6.99 |
| `--ds-err` | 6.54 | 6.05 | 7.26 | 6.94 |
| `--ds-focus` | 7.84 | 7.25 | 7.66 | 7.32 |
| `--ds-line-strong` | 4.56 | 4.22 | 4.60 | 4.39 |
| `--ds-series-1` | 4.60 | 4.26 | 7.10 | 6.79 |
| `--ds-series-2` | 5.07 | 4.69 | 9.75 | 9.32 |
| `--ds-series-3` | 4.95 | 4.57 | 9.46 | 9.05 |
| `--ds-series-4` | 5.26 | 4.86 | 6.27 | 5.99 |
| `--ds-series-5` | 4.80 | 4.44 | 7.63 | 7.30 |
| `--ds-on-accent` on `--ds-accent` | 7.84 | 7.84 | 7.66 | 7.66 |
<!-- contrast:end -->

### Verification

From the repository root with the normal toolchain:

```sh
ci
```

Targeted advisory checks in a guest without `ci` (Linux PR CI remains required):

```sh
pnpm install --frozen-lockfile --ignore-scripts
pnpm --dir projects/monolith/frontend exec vitest run src/lib/design-system/technical-drawing.test.js src/lib/design-system/technical-drawing.dom.test.js
pnpm --dir projects/monolith/frontend exec vitest run --config factory-preview/vitest.config.js
pnpm --dir projects/monolith/frontend exec vitest run --config theme-preview/vitest.config.js
FACTORY_PREVIEW_SHA=$(git rev-parse HEAD) pnpm --dir projects/monolith/frontend exec vite build --config theme-preview/vite.config.js
```

The Node test checks completeness, scoped syntax, default values, palette order,
contrast, documentation and import safety. The happy-dom test loads both real
package exports and checks explicit selection, nesting, closed-boundary siblings,
unknown theme values, defaults and import-order independence. These are contract
checks. The separate theme fixture tests server-render without browser globals
and hydrate the same markup without replacing nodes, text or attributes.
Linux CI covers the private and public frontend builds, existing Factory/blog
fixtures, and the isolated theme targets. The existing Factory mobile preview
workflow also runs the theme browser checker at 320px and 1440px in both schemes
with 100% and actual 200% text sizes. It uploads computed contrast and screenshots
in `theme-preview-evidence-<head sha>`. Browser prerequisites and the standalone
check command are in `frontend/theme-preview/README.md`.

## Rules that already hold

- Themes scope conflicting values to a surface marker or component root
  (`.public-theme` from the public layout and error boundary, `.ember-site`,
  `.grimoire`). Do not add another unscoped writer for those names.
- A token change here ships in the shared monolith frontend image, so it
  bumps both `monolith` and `monolith-public` together. Ownership moved here;
  release trains did not.
- Do not converge the themes visually. The differentiation is a product
  decision recorded in the stylesheets and in `.impeccable.md`, not drift.

## Decision status

| Decision | Status | Claimed by |
| --- | --- | --- |
| ADR platform/013, shared contract with three distinct themes | Superseded by the collision-only scope in #4449; distinct themes retained | shared with the platform rollup; not deleted here |

Issues: #4449 (collision isolation and programme supersession), #4667 (this
domain is recorded there as README-only, no `ARCHITECTURE.md`).
