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
├── package.json          @homelab/design-system, exports tokens/contract.css
├── BUILD                 js_library linked via npm_link_all_packages (hand-maintained)
└── tokens/contract.css   the --ds-* roles, defined once at :root
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
  Svelte or CSS file in the frontend reads a `var(--ds-*)` token, and none of
  the three themes overrides a `--ds-*` token inside its scope class.
- The five conflicting public tokens are scoped to
  `body:has(.public-theme)`, while the shared defaults remain at `:root`.
  Public palette selection therefore follows route ancestry rather than
  stylesheet import order.
- There is no primitive layer, no `jomcgi.dev/design` gallery, and no
  Storybook, and #4449 does not require any of them.

The live styling rules remain the three per-theme stylesheets named in
`.impeccable.md`, and that file is the document to follow when touching any
of them. The broader contract migration and primitive programme are no longer
outstanding work. This preserves the package history without turning it into
an adoption requirement.

## Rules that already hold

- Themes scope conflicting values to their route or component root
  (`.public-theme`, `.ember-site`, `.grimoire`). Do not add another
  unscoped writer for those names.
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
