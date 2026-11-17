# Factory acceptance evidence

Local checks on 2026-11-17 cover the site tree at `99ca767349b12653c323e962c6aa211987fd6dbf`
for HOME-224, PR #8197, with Node 22.23.5, pnpm 10.34.5 and locked dependencies.
Subsequent changes to this record affect documentation only.

## The storyboard is part of the site

The [registry](../web/src/pages.tsx) and [Vite entries](../web/vite.config.ts)
include `factory/index.html` in the existing pre-rendered site build.
The [Factory component](../web/src/factory-page.tsx) selects client routes;
[_redirects](../web/public/_redirects) serves the same shell on descendants.

| Route | Content in [Pages.tsx](../web/src/factory/Pages.tsx) | Fixture assertion in [App.test.tsx](../web/src/factory/App.test.tsx) |
| --- | --- | --- |
| `/factory` | `StoryPage`: headline, goal chapters and progress, stats, four charts, work in progress and recently shipped | Four chart tables, unknown stat substitution and both work lists |
| `/factory/goals/reading-list` | `GoalPage`: chapter, issues by state and burn-up | Every issue state, burn-up, and the empty `quiet-shelf` goal |
| `/factory/issues/DEMO-104` | `IssuePage`: creation, turns, PR, reviews, checks, queue and merge in one timeline | Both review rounds, failed and passed checks, queue entry, PR and session links |
| `/factory/sessions/demo-build` | `SessionPage`: transcript, folded tools and inline diffs | 28 turns, withheld content, closed tool details and the literal diff |

The [reader](../web/src/factory/data.ts) uses the HOME-68 types and JSON Schema,
verifies hashes and polls `/data/factory/manifest.json` every 60 seconds.
The schema, types and fixture bytes have no diff against the merge base.

## Criterion 1: fixture and contract tests

[`web:test`](../web/BUILD) includes the following through `test_src` and
`src/**/*.test.tsx`, with the shared schema and fixture dependencies.

| Required case | Checkable assertion |
| --- | --- |
| Every page renders from fixtures | `App.test.tsx`, `renders the fixture page at %s`, checks four routes and expected headings. [schema.test.tsx](../web/src/factory/schema.test.tsx) checks every fixture, hash and cross-link. |
| Unknown stat placeholder | [data.test.tsx](../web/src/factory/data.test.tsx), `fills repeated, zero and fractional stats and uses a dash for unknown keys`, checks exact output. |
| Malformed object preserves other content | `App.test.tsx`, `shows a malformed issue inline and keeps sibling issues visible`, requires `Issue DEMO-101 not available.` and the valid DEMO-104 link together. |
| Script-looking fixture text stays text | `App.test.tsx`, `renders withheld turns, folded tool calls and inline diffs as text`, requires the literal `<script>alert("fixture")</script>` and no script element. |
| Axe on each page | [contracts.test.tsx](../web/src/factory/contracts.test.tsx) checks the four fixture routes in both themes, with chart tables and tool details open. |
| CSP and palette | `contracts.test.tsx` rejects inline style elements and attributes and checks `style-src 'self'`. [contrast.test.tsx](../web/src/contrast.test.tsx) scans nested CSS and TypeScript, restricts colours to `palette.css`, and checks both themes' token contrast. |

Axe disables `color-contrast` because happy-dom has no layout; token contrast
has separate assertions. [serving.test.tsx](../web/src/factory/serving.test.tsx)
checks route shells, exact fixture bytes and JSON 404s for invalid data paths.

## Criterion 2: the built Factory entry is within budget

From `projects/jomcgi-site/web`:

```sh
pnpm test
pnpm typecheck
pnpm build
pnpm budget
node factory-built.test.mjs dist
```

All commands exited 0: **254 tests passed in 15 files**, plus four artifact tests.
The Factory entry measured **110,848 bytes (108.3 KiB)** of initial gzipped
JavaScript against **153,600 bytes (150 KiB)**.

[`budget.mjs`](../web/budget.mjs), used by `web:budget_test`, counts scripts
and module preloads in every HTML entry. The [artifact test](../web/factory-built.test.mjs)
also requires a nonempty Factory entry within budget, the pre-rendered shell,
local assets, the rewrite and no fictional production data.

The combined build also passed `prerendered.mjs` (including both blog posts),
`marketplace.test.mjs` and all seven `scotland-built.test.mjs` checks.
These are local results; remote Bazel checks run in PR CI.

## Scope and rollout

Against merge base `3539b94077e00547fad230e769fcf768d2b142cc`, `edge/routes.json`,
`chart/`, `deploy/` and `web/src/sections.ts` have no diff (criterion 3).
Factory remains `live: false`; HOME-213 supplies data and HOME-225 activates routes.
Criterion 4 remains pending: after dev rollout, the delivery owner checks
`https://next.jomcgi.dev/factory` and `https://next.jomcgi.dev/factory/goals/x`
for HTTP 200 and the rendered shell with unavailable states.
