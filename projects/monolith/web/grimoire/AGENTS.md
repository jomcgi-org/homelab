# AGENTS.md - Grimoire web app

Scoped guidance for `projects/monolith/web/grimoire`. The repo-root and
`projects/monolith` files still apply.

The friends UI at `friends.jomcgi.dev/grimoire`: a client-only React app built
from [Astryx](https://github.com/facebook/astryx) components. It is static
files. The browser calls the Grimoire API directly; the gateway handles login.
`projects/monolith/grimoire/architecture.md` records why.

## Adding a feature

1. **Backend first.** Add or change the route in `grimoire/router.py`, then
   regenerate the contract from `projects/monolith`:
   `python -m grimoire.browser_api_schema > web/grimoire/src/api/schema.ts`.
   A new path prefix must also be added to `grimoire/browser_lane.py`.
2. **API module.** Add a function in `src/api/<area>.ts` that calls
   `api('METHOD /path', ...)`. Operations, bodies and responses are typed by
   `schema.ts`, so a wrong shape fails typecheck.
3. **Screen.** Compose Astryx components. Reads use `useQuery` through
   `Loaded`; writes use `useAction`. `src/screens/lobby` is the reference.
4. **Fixtures.** Add the route to `src/test/world.ts` so the screen opens in
   the fixture preview.
5. **Test.** `openApp(routes, path)` from `src/test/app.tsx`, then query by role
   and name.

## Look a component up before writing one

```sh
pnpm exec astryx search <words>        # components, hooks, docs, templates
pnpm exec astryx component --list
pnpm exec astryx --dense component Dialog
pnpm exec astryx hook --list
```

Worked examples for every component are in
`node_modules/@astryxdesign/cli/assets/templates/blocks/components/<Name>/`.
Astryx is a beta: read the docs for the installed version rather than
recalling an API.

## Rules

`src/rules.test.ts` enforces these and names the replacement on failure.

- Overlays, comboboxes, menus and tabs come from Astryx. No hand-built roles.
- Outcomes are reported with `useToast()`, other announcements with
  `useAnnounce()`. No `aria-live`.
- No raw `<button>`, `<input>`, `<a>` or similar. Navigation uses `Link` or
  `Button href` with a path from `src/app/paths.ts`.
- `fetch` appears only in `src/api/client.ts`.
- Text renders as text. `src/handouts/HandoutText.tsx` is the only place that
  may inject HTML.
- Colours, spacing and radius come from theme tokens. No inline styles.

## Layout

| Path                  | Holds                                                       |
| --------------------- | ----------------------------------------------------------- |
| `src/api/`            | `client.ts`, the generated `schema.ts`, one module per area |
| `src/app/`            | Providers, `Page`, `Loaded`, `useAction`, sign-in state     |
| `src/screens/<name>/` | One directory per route                                     |
| `src/lib/`            | Framework-free logic with its own tests                     |
| `src/theme/`          | `sheetTheme.ts` and its compiled output                     |
| `src/test/`           | Fixture backend, fixtures, test entry                       |
| `preview/`            | Fixture preview entry                                       |

## Commands

```sh
pnpm test             # vitest, including the house rules
pnpm typecheck
pnpm build            # production bundle in dist/
pnpm preview:build    # fixture preview in dist-preview/; open index.html#/<route>
pnpm theme            # recompile src/theme/sheet.* after editing sheetTheme.ts
```

## Gotchas

- **Dependencies are a closed list.** Versions are exact pins and must be at
  least three days old (`minimumReleaseAge` in `pnpm-workspace.yaml`). A new
  dependency needs a sentence of justification in its PR.
- **`schema.ts` and `src/theme/sheet.*` are generated.** Never edit them. Both
  are in `.prettierignore`.
- **A toast renders its text twice**, once for screen readers. Tests use
  `findToast`.
- **A Selector's listbox stays in the DOM while closed.** Tests use
  `chooseOption`.
- **The app's base path is `GRIMOIRE_BASE`** (default `/grimoire/next/`). Paths
  in `src/app/paths.ts` are relative to it. Gateway paths such as sign-out are
  absolute and listed in `gateway`.
- **No server code.** No React Server Components, server actions or SSR.
