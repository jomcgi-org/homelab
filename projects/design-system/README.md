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
├── package.json                 @homelab/design-system, CSS, components and opt-in data-display exports
├── BUILD                        js_library linked via npm_link_all_packages (hand-maintained)
├── README.md                    role guide and tested contrast table
├── components/
│   ├── index.js                 named component exports
│   ├── Button.svelte            native button
│   ├── Field.svelte             native control composition
│   ├── Disclosure.svelte        native details/summary
│   ├── Tabs.svelte              local tablist and panels
│   ├── PageHeader.svelte        heading and action snippets
│   └── Breadcrumb.svelte        compact navigation
├── data-display/                Svelte 5 presentation primitives and plain JS contracts
│   ├── index.js                 component entry (svelte condition)
│   ├── core.js                  formatter, contracts and synthetic fixtures (default condition)
│   ├── format.js                compact and exact measurements, explicit locale
│   ├── contracts.js             status meanings, content states and ordered series roles
│   ├── fixtures.js              immutable synthetic edge values
│   └── *.svelte                 Panel/Section, KeyValue, Status, Metric, ChartFrame, Legend
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

The contract is wired; production pages do not consume the shared primitives.
As of this README:

- The package is linked and the stylesheet loads on every route, but no
  production Svelte or CSS file in the frontend reads a `var(--ds-*)` token, and none of
  the three themes overrides a `--ds-*` token inside its scope class. The new
  technical-drawing export is opt-in and not yet consumed by any page.
- The five conflicting public tokens are scoped to
  `body:has(.public-theme)`, while the shared defaults remain at `:root`.
  The public layout and root error boundary mount that marker, so the
  brutalist palette follows the rendered surface rather than stylesheet
  import order.
- Opt-in controls and navigation primitives exist under `components/`, with
  synthetic SSR/hydration fixtures. No production page consumes them. There is
  no `jomcgi.dev/design` gallery or Storybook. #4449 requires no migration.
- #6875 adds an opt-in data-display primitive set under `data-display/`,
  exercised by synthetic fixtures only. No production page consumes it.

The live styling rules remain the three per-theme stylesheets named in
`.impeccable.md`, and that file is the document to follow when touching any
of them. The broader contract migration and primitive programme are no longer
outstanding work. This preserves the package history without turning it into
an adoption requirement.

## Controls and navigation

Import named components from `@homelab/design-system/components`, or a single
default export from `@homelab/design-system/components/Button.svelte` (replace
`Button` with any name below). Load `tokens/contract.css`; opt into the
technical-drawing export at explicit light/dark boundaries as shown below.
The three existing CSS exports and both token files are unchanged.

These are Svelte 5 presentational primitives. They have no fetch, store,
persistence, route state, private/server import, or module-scope browser access.
Callers own validation, submission, navigation and application state. Component
styles use scoped `--ds-*` roles; theme-only focus/link roles fall back to the
contract roles outside a boundary. Technical drawing uses square corners and no
shadow. Interactive targets have 44px minimum width/height, labels wrap, and
there are no transitions or animations. Reduced-motion rules also suppress both
on interactive elements. Browser measurements are separate from Node semantics.

| Component | Props and snippets | HTML and behavior |
| --- | --- | --- |
| `Button` | `type="button"`, `disabled=false`, `variant="primary"`, `children()`; other props forwarded | Native `<button>` with explicit type. `submit` and `reset` are opt-in; missing, null or invalid types normalize to `button`. Provide visible children or `aria-label`. `primary`, `secondary`, `quiet` variants; dashed disabled border and native non-activation. `onclick`, `form`, `name`, `value`, `aria-*` and `class` pass through. Enter/Space use native activation. |
| `Field` | Required `label` and `control(attributes)`; optional `description`, `error`, `required=false`, `disabled=false` | `<label for>` and optional description/error paragraphs around the caller's native input/select/textarea. Spread **all** snippet attributes onto exactly one control. `$props.id()` supplies stable unique IDs. `aria-describedby` includes description and error IDs; errors set `aria-invalid="true"` and start with `Error:`. Required adds `(required)` and native `required`; disabled passes native `disabled`. No validation or form-state engine. |
| `Disclosure` | Required `summary()`; optional `children()`, bindable `open=false` | Native `<details>/<summary>`. Enter/Space toggle, Tab follows native document order, no JavaScript needed for toggling. A decorative plus becomes minus when open. `bind:open` synchronizes native toggles and caller changes. |
| `Tabs` | Required `tabs: { id, label, disabled? }[]`, `panel(tab)`, and either `label` or `labelledby`; `orientation="horizontal"`, bindable `selected`, `onchange(id)` | Labelled `tablist`; button `tab`s with `aria-selected`/`aria-controls`; `tabpanel`s with `aria-labelledby`, `tabindex=0`, inactive `hidden`. Stable unique tab IDs are caller data; DOM IDs are SSR-stable and instance-local. Selection defaults to first enabled tab. An unknown/disabled selection falls back there without changing the caller value or emitting an event. |
| `PageHeader` | Required `title`; `level=1` (1 to 6), optional `description`, `breadcrumb()`, `actions()` | `<header>` with `h1` to `h6`, optional description and caller-supplied navigation/actions. Actions and long headings wrap. No masthead, routing or imposed action. |
| `Breadcrumb` | `items: { label, href?, current? }[]`, optional `label="Breadcrumb"` | Labelled `<nav><ol>` with native links when `href` exists, otherwise text. Set `current: true` on exactly one item for `aria-current="page"`. Decorative separators are hidden from assistive technology. Links have 44px hit areas and wrap without truncation; Tab/Enter use native navigation. |

Tabs use **automatic activation**: focus movement selects immediately. Horizontal
Left/Right or vertical Up/Down wrap through enabled tabs; Home/End select the
first/last enabled tab. Disabled tabs are native-disabled and skipped. Exactly
one enabled tab has `tabindex=0`; the others have `-1`. A single enabled tab
stays selected through every navigation key, with no duplicate `onchange`.
An empty or entirely disabled list has no tab stop or visible panel; callers
should normally provide at least one enabled tab. Tab/Shift+Tab enter/leave the
tablist through the selected tab; panel content follows in native document order.
Focusable panels also have a 44px minimum width and height.
Selected state has an underline, stronger rule and weight. Enter/Space activate
the focused native button. There is no manual activation mode.

`onchange` fires once for a user selection change and receives the selected tab
ID. Setting `selected` externally does not emit it. Routing synchronization is
the caller's responsibility: initialize `selected` from the caller's route and
handle `onchange` there. Components do not read or write URLs.

```svelte
<script>
  import { Breadcrumb, Button, Disclosure, Field, PageHeader, Tabs }
    from "@homelab/design-system/components";
  import "@homelab/design-system/tokens/contract.css";
  import "@homelab/design-system/tokens/technical-drawing.css";
  let selected = $state("overview");
  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "notes", label: "Notes" },
  ];
</script>

<section data-ds-theme="technical-drawing-light">
  <PageHeader title="Drawing sheet" description="Local example">
    {#snippet breadcrumb()}
      <Breadcrumb items={[
        { label: "Sheets", href: "/sheets" },
        { label: "Drawing sheet", current: true },
      ]} />
    {/snippet}
    {#snippet actions()}<Button onclick={() => {}}>Print sheet</Button>{/snippet}
  </PageHeader>
  <form>
    <Field label="Sheet size" description="Choose the native select option" required>
      {#snippet control(attributes)}
        <select {...attributes} name="size"><option>A4</option><option>A3</option></select>
      {/snippet}
    </Field>
    <Button type="submit">Save sheet</Button>
  </form>
  <Disclosure>
    {#snippet summary()}Sheet details{/snippet}
    <p>Local details</p>
  </Disclosure>
  <Tabs {tabs} label="Sheet panels" bind:selected>
    {#snippet panel(tab)}<p>{tab.label} content</p>{/snippet}
  </Tabs>
</section>
```

The synthetic `projects/monolith/frontend/controls-preview/` fixture exercises
these APIs through package exports. Its contract suite server-renders without
browser globals, hydrates with element/text/attribute identity and zero console
warnings/errors, exercises callbacks/bindings, and checks shared Svelte runtime
resolution. Native tab order, disclosure activation, visual focus, hit areas,
contrast, nested theme isolation, reduced motion and layout require its Chromium
checks. No production page imports these primitives.

## Opt-in data display

Import components from `@homelab/design-system/data-display`. Its `svelte`
condition exports the components and the JS contracts. Plain Node resolves the
`default` condition to `core.js`, which exports only the formatter, contracts
and fixtures and needs no Svelte compiler. The three CSS exports and the
default `.` export still resolve to their original stylesheets.

All component styles are scoped and use `--ds-*` roles. Import the contract
stylesheet and select an explicit technical-drawing boundary for series
colours. Nested boundaries inherit their own roles. Outside a boundary the
legend falls back to ink; labels and distinct shapes retain meaning.
There are no application imports, stores, requests or browser-global reads.

### Components

| Component | Props and snippets | Semantics |
| --- | --- | --- |
| `Panel` (also exported as `Section`) | Required `title`; `headingLevel=2` (integer 2 through 6); `state="ready"`; optional `message`, `children()` and `footer()` snippets | Outlined native section linked to a real heading. Footer partition remains visible in every state. |
| `KeyValue` | `rows=[]` of `{label, value, unit?}`; `density="dense"` or `"sparse"`; optional `value(row)` snippet | Native definition list. Values flow from the left and wrap; rows stack below 30rem. Null, undefined and non-finite numeric values display Unavailable. A custom snippet owns its value semantics. |
| `Status` | `kind="unknown"`; required `label`; `live=false` | Stable cue and visible human label. Ordinary rendering has no live region. `live=true` opts into `role="status"` and polite announcements. |
| `Metric` | Required `label`; `value`; `unit=""`; `locale="en-US"`; optional `context`; `state="ready"` | Labelled group, compact visible value with accessible exact text including units, plus native Exact value disclosure for sighted keyboard/touch users. |
| `ChartFrame` | Required nonblank `title`, `units`, `description` and `fallback(state)` snippet; optional `children()` chart snippet, `message`; `state="ready"` | Native figure linked to title and description in its visible figcaption. Read chart data disclosure renders the fallback in every state. No chart renderer. |
| `Legend` | `entries=SERIES_ROLES` (array of `{id, label}`); `label="Chart series"` | Labelled native list. Supplied entries are sorted into contract order without mutation, with visible shape names and distinct SVG markers. Unknown/duplicate IDs and blank labels throw. An empty array is an empty list. |

Required text inputs reject missing, non-string or whitespace-only values with
`TypeError` during render. Invalid headings, densities, kinds and content states
throw `RangeError`. ChartFrame checks its metadata and fallback even while
loading or unavailable. The caller must supply meaningful descriptive text and
usable fallback content, such as a captioned table with column and row headers;
the component validates snippet presence, not the caller's prose or table schema.
Style caller-owned tables with wrapping cells and a width bounded by the figure.

`CONTENT_STATES` maps `ready`, `loading`, `empty`, `error`, `unavailable` to
human text. Panel and ChartFrame render chart/content snippets only when ready;
other states render state text and optional message. Metric renders no stale
value in those states. Missing or non-finite ready measurements become
Unavailable. Loading sets `aria-busy`; none of these ordinary data states is a
live region. Disclosures use native keyboard behavior, a minimum 44px height
and a visible `--ds-focus` outline using `--ds-focus-width`.

### Formatting and stable meanings

`formatMeasurement(value, {unit="", locale=DEFAULT_LOCALE})` returns a frozen
`{state, text, exactText, unit, locale}`. `MEASUREMENT_STATES` is `AVAILABLE:
"available"`, `UNAVAILABLE: "unavailable"`. Only finite numbers are available:
zero, negatives and fractions are measurements; strings, missing and non-finite
inputs are never coerced into zero. Compact text uses three significant digits;
exact text preserves the JavaScript number with up to 21 significant digits
and appends literal units. No extra measurement precision is invented.

`DEFAULT_LOCALE` is deterministic `en-US`, never navigator or the ambient host
locale. Pass the same explicit locale during SSR and hydration. Valid unsupported
tags fall back to `en-US`; malformed tags throw `RangeError`, and empty/non-text
locale or non-text unit inputs throw `TypeError`. Exact and compact formatting
both use the resolved locale. The same Intl locale data must be available on
the server and client.

`STATUS_KINDS` freezes each label, meaning, CSS role and cue: `ok` means healthy
or successful (`--ds-ok`, check); `warn` means attention required (`--ds-warn`,
triangle); `err` means failure (`--ds-err`, cross); `unknown` means not known or
unavailable (`--ds-ink-muted`, question mark); `pending` means waiting or loading
(`--ds-ink-muted`, clock). Supply a domain label that preserves that meaning.
The cue is hidden from screen readers; the explicit label carries the meaning.

`SERIES_ROLES` is frozen in this order in both schemes:

| ID | Default label | Role | Marker |
| --- | --- | --- | --- |
| `gpu` | GPU | `--ds-series-1` | circle |
| `host-ram` | Host RAM | `--ds-series-2` | square |
| `page-cache` | Page cache | `--ds-series-3` | triangle |
| `nvme` | NVMe | `--ds-series-4` | diamond |
| `hot-expert-set` | Hot expert set | `--ds-series-5` | cross |

`DATA_DISPLAY_FIXTURES` exports frozen measurements (all numeric edges,
missing and non-finite values, long labels/units), rows, densities and states.
They contain synthetic constants only. Unit/DOM tests live in
`projects/monolith/frontend/src/lib/design-system/`; the isolated theme preview
renders every component in light, dark and nested boundaries and checks SSR,
hydration and real Chromium text resize. No production page consumes this set.

### Composition

```svelte
<script>
  import "@homelab/design-system/tokens/contract.css";
  import "@homelab/design-system/tokens/technical-drawing.css";
  import { Panel, KeyValue, Status, Metric, ChartFrame, Legend }
    from "@homelab/design-system/data-display";
</script>

<section data-ds-theme="technical-drawing-light">
  <Panel title="Synthetic memory">
    <KeyValue rows={[{label: "Source", value: "Synthetic"}]} />
    <Status kind="unknown" label="Synthetic sample, health unknown" />
    <Metric label="GPU memory" value={0} unit="bytes" locale="en-US" />
    <ChartFrame title="Memory sample" units="bytes"
      description="One synthetic GPU measurement, zero bytes.">
      <Legend entries={[{id: "gpu", label: "GPU"}]} />
      {#snippet fallback(state)}
        <table>
          <caption>Memory sample ({state})</caption>
          <thead><tr><th scope="col">Series</th><th scope="col">Bytes</th></tr></thead>
          <tbody><tr><th scope="row">GPU</th><td>0</td></tr></tbody>
        </table>
      {/snippet}
    </ChartFrame>
  </Panel>
</section>

<style>
  table { width: 100%; table-layout: fixed; }
  th, td { text-align: start; overflow-wrap: anywhere; }
</style>
```

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
