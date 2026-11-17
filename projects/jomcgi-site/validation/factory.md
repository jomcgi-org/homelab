# Factory acceptance evidence

Local checks on 2026-11-17 used the runtime and tests at
`d1385f092e90c5338c297f62783d0acc3b984a04` for HOME-224, PR #8197.
This record and its README link change documentation only.
The commands below used Node 22.23.5, pnpm 10.34.5 and the locked dependencies.

## The storyboard is part of the site

The [page registry](../web/src/pages.tsx) registers `factory/index.html`.
The [Vite entries](../web/vite.config.ts) include that file in the existing
site build. [build.mjs](../web/build.mjs) pre-renders the registered pages.
The existing [site_tar target](../BUILD) packages the complete build for deployment.

The [Factory component](../web/src/factory-page.tsx) hydrates that shell, then
selects the client route. The built [_redirects](../web/public/_redirects)
maps `/factory` and `/factory/*` to `/factory/`.

| Route | Content in [Pages.tsx](../web/src/factory/Pages.tsx) | Fixture assertion in [App.test.tsx](../web/src/factory/App.test.tsx) |
| --- | --- | --- |
| `/factory` | `StoryPage`: headline, goal chapters and progress, stats, four charts, work in progress and recently shipped | Four chart tables, unknown stat substitution and both work lists |
| `/factory/goals/reading-list` | `GoalPage`: chapter, issues by state and burn-up | Every issue state, burn-up, and the empty `quiet-shelf` goal |
| `/factory/issues/DEMO-104` | `IssuePage`: creation, turns, PR, reviews, checks, queue and merge in one timeline | Both review rounds, failed and passed checks, queue entry, PR and session links |
| `/factory/sessions/demo-build` | `SessionPage`: transcript, folded tools and inline diffs | 28 turns, withheld content, closed tool details and the literal diff |

The navigation test follows story, goal, issue and session links, then returns
to the issue. It checks focus and browser history.
[Charts.tsx](../web/src/factory/Charts.tsx) renders SVG and value tables.

The [reader](../web/src/factory/data.ts) imports the HOME-68 types.
The [validator](../../factory-storyboard/schema/validate.ts) reads the HOME-68
JSON Schema. The schema, types and fixture bytes have no diff against the merge base.
It requests `/data/factory/manifest.json`, then the manifest's object paths.
It verifies schema and byte hashes before rendering, and polls every 60 seconds.
[data.test.tsx](../web/src/factory/data.test.tsx) checks request order,
cached objects, rejected manifests and isolated object failures.
[App.test.tsx](../web/src/factory/App.test.tsx) checks polling and recovery.
The [development middleware](../web/factory-fixtures.ts) serves the original
[fixtures](../../factory-storyboard/fixtures/manifest.json) at those same paths.

## Criterion 1: fixture and contract tests

The [`web:test` target](../web/BUILD) includes `src/**/*.test.tsx` through
`test_src`, with the shared schema and fixture dependencies.
The [test configuration](../web/vitest.config.ts) includes the same files.
These assertions run in `//projects/jomcgi-site/web:test`:

| Required case | Checkable assertion |
| --- | --- |
| Every page renders from fixtures | `App.test.tsx`, `renders the fixture page at %s`, checks four routes and their expected headings. `schema.test.tsx` validates every fixture, hash and cross-link. |
| Unknown stat placeholder | `data.test.tsx`, `fills repeated, zero and fractional stats and uses a dash for unknown keys`, checks the exact output. `App.test.tsx` checks the fixture's substituted sentence. |
| Malformed object preserves other content | `App.test.tsx`, `shows a malformed issue inline and keeps sibling issues visible`, requires `Issue DEMO-101 not available.` and the valid DEMO-104 link together. |
| Script-looking fixture text stays text | `App.test.tsx`, `renders withheld turns, folded tool calls and inline diffs as text`, requires the literal `<script>alert("fixture")</script>` and no script element. |
| Axe on each page | [contracts.test.tsx](../web/src/factory/contracts.test.tsx) checks the four fixture routes in both themes, with chart tables and tool details open. |
| CSP and palette | `contracts.test.tsx` rejects inline style elements and attributes and checks `style-src 'self'`. [contrast.test.tsx](../web/src/contrast.test.tsx) scans nested CSS and TypeScript, restricts colours to `palette.css`, and checks both themes' token contrast. |

The axe tests disable `color-contrast` because happy-dom has no layout.
Token contrast has separate assertions; these tests do not measure browser layout.
[serving.test.tsx](../web/src/factory/serving.test.tsx) checks shell responses
on all route shapes, exact fixture bytes, and invalid data paths returning JSON 404.

From the repository root:

```sh
pnpm --dir projects/jomcgi-site/web test
pnpm --dir projects/jomcgi-site/web typecheck
```

Observed: **208 tests passed in 14 files**, including the cases above.
Typechecking exited 0.

## Criterion 2: the built Factory entry is within budget

From `projects/jomcgi-site/web`:

```sh
pnpm build
pnpm budget
node factory-built.test.mjs dist
```

All three commands exited 0. The four artifact tests passed.
The Factory entry measured **110,162 bytes (107.6 KiB)** of initial gzipped
JavaScript against **153,600 bytes (150 KiB)**.

| File referenced by `dist/factory/index.html` | Gzipped bytes |
| --- | ---: |
| `/assets/factory-DW3GyR2w.js` | 6,982 |
| `/assets/Shell-DfxBIlwk.js` | 103,180 |
| Total | 110,162 |

[`budget.mjs`](../web/budget.mjs) measures every HTML entry recursively,
including executable scripts and module preloads.
`//projects/jomcgi-site/web:budget_test` runs that script on `:build`.
The [Factory artifact test](../web/factory-built.test.mjs) also requires
`factory/index.html` to exist, have nonzero initial JS, and stay within budget.
It checks the pre-rendered unavailable states, local assets, rewrite and absence
of fictional data from production output.

GitHub reported `pr-checks` successful for the tested commit:
[commit status](https://github.com/jomcgi-org/homelab/commit/d1385f092e90c5338c297f62783d0acc3b984a04/checks),
[BuildBuddy invocation](https://jomcgi.buildbuddy.io/invocation/888abfa6-1c39-4bfc-8e86-502997fe3042).
The target-level remote log was unavailable from this sandbox.
The counts and sizes above come from the local commands, not that unread log.

## Scope and rollout

Criterion 3 was checked against merge base `415d8be7dd8761f9d27cb1359fcd15fc673e3dbc`:
`edge/routes.json`, `chart/`, `deploy/` and `web/src/sections.ts` have no diff.
Factory remains `live: false`. HOME-213 supplies production data;
HOME-225 owns the production edge routes and legacy redirects.

Criterion 4 requires the dev rollout. The delivery owner checks both
`https://next.jomcgi.dev/factory` and `https://next.jomcgi.dev/factory/goals/x`
for HTTP 200 and the rendered shell with unavailable states.
That live verification remains pending. Local shell tests do not verify
Cloudflare routing or the deployed revision.
