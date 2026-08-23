# Design system

The shared `--ds-*` token contract the frontends are meant to build against:
one namespaced vocabulary, three deliberately distinct themes (neobrutalist
public tier, ember mini-site, Grimoire) that will override it inside their
own scope class and never at `:root`.

This directory is the package. It is not the design documentation. For how
the three themes look, why they are kept apart, the `--accent` import-order
trap, and the rules a new surface has to respect, read **`.impeccable.md`**
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

The contract is **wired but not yet consumed**. As of this README:

- The package is linked and the stylesheet loads on every route, but no
  Svelte or CSS file in the frontend reads a `var(--ds-*)` token, and none of
  the three themes overrides a `--ds-*` token inside its scope class.
- The two unscoped `:root` token sets the ADR set out to fix
  (`lib/public/styles/design-system.css` and `lib/styles/shared/tokens.css`)
  still both write the same unnamespaced names, so the import-order trap in
  `.impeccable.md` is still live.
- There is no primitive layer, no `jomcgi.dev/design` gallery, and no
  Storybook.

So the live styling rules are still the three per-theme stylesheets named in
`.impeccable.md`, and that file is the document to follow when touching any
of them. Every remaining step is tracked on #4449 in dependency order; step 2
(stand up this package) is the only one done.

## Rules that already hold

- Themes scope their overrides to their own class (`.ember-site`,
  `.grimoire`); the public baseline is the one writing `:root`. Do not add a
  fourth `:root` writer.
- A token change here ships in the shared monolith frontend image, so it
  bumps both `monolith` and `monolith-public` together. Ownership moved here;
  release trains did not.
- Do not converge the themes visually. The differentiation is a product
  decision recorded in the stylesheets and in `.impeccable.md`, not drift.

## Decisions and outstanding work

| Decision | Status | Claimed by |
| --- | --- | --- |
| ADR platform/013, shared contract with three distinct themes | Accepted, partially executed (#4449) | shared with the platform rollup; not deleted here |

Issues: #4449 (the implementation tracking issue), #4667 (this domain is
recorded there as README-only, no `ARCHITECTURE.md`).
