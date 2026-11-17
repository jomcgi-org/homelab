# Factory acceptance evidence

Local checks for HOME-224, PR #8197, on 2026-11-17 use Node 22.23.5,
pnpm 10.34.5 and locked dependencies. Remote Bazel checks run in PR CI.

## The storyboard is part of the site

The [registry](../web/src/pages.tsx) and [Vite entries](../web/vite.config.ts) build
`factory/index.html`; [_redirects](../web/public/_redirects) serves that shell on
descendants. [App.test.tsx](../web/src/factory/App.test.tsx) hydrates the site's
registered shell at all four fixture routes, with no recoverable hydration errors.

| Route | Content in [Pages.tsx](../web/src/factory/Pages.tsx) | Fixture assertion in [App.test.tsx](../web/src/factory/App.test.tsx) |
| --- | --- | --- |
| `/factory` | `StoryPage`: headline, goal chapters and progress, stats, four charts, work in progress and recently shipped | Four chart tables, unknown stat substitution and both work lists |
| `/factory/goals/reading-list` | `GoalPage`: chapter, issues by state and burn-up | Every issue state, burn-up, and the empty `quiet-shelf` goal |
| `/factory/issues/DEMO-104` | `IssuePage`: creation, turns, PR, reviews, checks, queue and merge in one timeline | Both review rounds, failed and passed checks, queue entry, PR and session links |
| `/factory/sessions/demo-build` | `SessionPage`: transcript, folded tools and inline diffs | 28 turns, withheld content, closed tool details and the literal diff |

The [reader](../web/src/factory/data.ts) validates HOME-68 schema and hashes,
fetches `/data/factory/` on this origin and polls the manifest every 60 seconds.

## Criterion 1: fixture and contract tests

| Required case | Checkable assertion |
| --- | --- |
| Every page renders from fixtures | `App.test.tsx`, `hydrates the site's pre-rendered Factory shell from fixtures at %s`, checks all four headings and document titles. [schema.test.tsx](../web/src/factory/schema.test.tsx) checks every fixture, hash and cross-link. |
| Unknown stat placeholder | [data.test.tsx](../web/src/factory/data.test.tsx), `fills repeated, zero and fractional stats and uses a dash for unknown keys`, checks exact output. |
| Malformed object preserves other content | `App.test.tsx`, `shows a malformed issue inline and keeps sibling issues visible`, requires `Issue DEMO-101 not available.` and the valid DEMO-104 link together. |
| Script-looking fixture text stays text | `App.test.tsx`, `renders withheld turns, folded tool calls and inline diffs as text`, requires the literal `<script>alert("fixture")</script>` and no script element. |
| Axe on each page | [contracts.test.tsx](../web/src/factory/contracts.test.tsx) checks the four fixture routes in both themes, with chart tables and tool details open. |
| CSP and palette | `contracts.test.tsx` rejects inline style elements and attributes and checks `style-src 'self'`. [contrast.test.tsx](../web/src/contrast.test.tsx) scans nested CSS and TypeScript, restricts colours to `palette.css`, and checks both themes' token contrast. |

[`web:test`](../web/BUILD) includes these tests and fixture inputs. Axe disables
`color-contrast` in happy-dom; token contrast has separate assertions.

## Criterion 2: the built Factory entry is within budget

Supplied HOME-224 criterion:

> `:budget_test` passes with the `/factory` entry inside the 150 KiB initial-JS budget.

From `projects/jomcgi-site/web`, `pnpm build` then `pnpm budget` exited 0
at `4585c387a275156fb4ca74958ac9dccb3c8a3160`.
Factory measured **110,848 bytes (108.3 KiB)** against **153,600 bytes (150 KiB)**.
[`web:budget_test`](../web/BUILD) and `pnpm budget` both pass `factory/index.html`
to [budget.mjs](../web/budget.mjs): missing, empty or oversized entries fail.
[budget.test.tsx](../web/src/budget.test.tsx) exercises these CLI failures and a pass.
The budget counts initial scripts and preloads; all 12 built entries passed.

GitHub reports [`pr-checks` successful](https://github.com/jomcgi-org/homelab/commit/4585c387a275156fb4ca74958ac9dccb3c8a3160/status-details?popover=true)
for that head ([BuildBuddy run](https://jomcgi.buildbuddy.io/invocation/f3afb16c-34e3-4197-93ea-15bde351be6d)).
The run log was unavailable here (`ENOTFOUND`); this records GitHub's reported status.

At the same revision, `pnpm test` passed **258 tests in 15 files** and
`pnpm typecheck` passed. Three Factory artifact tests check the shell, local
assets, rewrite and no fictional data. They, `prerendered.mjs`,
`marketplace.test.mjs` and seven Scotland artifact checks passed.

## Scope and rollout

Against merge base `3539b94077e00547fad230e769fcf768d2b142cc`, `edge/routes.json`,
`chart/`, `deploy/` and `web/src/sections.ts` have no diff (criterion 3).
Factory remains `live: false`; HOME-213 supplies data and HOME-225 activates routes.
The goal and criterion 4 remain pending: after dev rollout, the delivery owner checks
`https://next.jomcgi.dev/factory` and `https://next.jomcgi.dev/factory/goals/x`
for HTTP 200 and the rendered shell with unavailable states.
