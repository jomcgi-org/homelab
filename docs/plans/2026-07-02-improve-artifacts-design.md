# improve-artifacts: UI/UX quality feedback loop for goosecracker artifacts

Date: 2026-07-02
Status: Approved design

## Problem

The artifact pipeline has three UI/UX layers, all feed-forward: the DESIGN BAR
section in `artifact-build.yaml`, the fresh-eyes pass in `artifact-review.yaml`,
and the static retry gates (JS parse, invalid Tailwind scale steps,
directive-without-runtime). Nothing feeds back from published artifacts to the
recipes.

The existing `/improve-recipes` loop cannot fill this gap. It selects the worst
sessions by `wall_seconds`, `owner_turns`, and failure states, so a session that
runs fast, needs no follow-ups, and publishes generic AI slop never enters its
worklist. Its taxonomy has no design-quality failure mode, and its deep-read is
transcript-based (sessions.db); it never fetches or renders the published HTML.
Selection bias determines what a feedback loop can ever fix: a cost-metric loop
converges on efficiency and cannot see quality regressions that cost nothing.

## Decision

Add a separate on-demand skill, `/improve-artifacts`, as a sister to
`/improve-recipes`: same helper-script data path, same evidence discipline, but
its own sampling frame (all published artifacts in the window, not worst
sessions) and its own judge (screenshots, not transcripts).

Decisions made during brainstorming:

- Judge input: screenshots via a local headless browser (playwright/chromium),
  desktop and mobile widths. Strongest signal; sees what a user sees.
- Sampling: every artifact published since the last commit touching the recipes
  dir (same window convention as improve-recipes, so before/after windows line
  up). Cap around 15, newest first, if a window is unusually busy.
- Editable surface: both artifact recipe YAMLs, prose and static retry gates.
  Recurring mechanically-detectable defects get promoted to gates (the
  slate-705 path); this is the loop's highest-leverage output.
- Cadence: on-demand only. Screenshots need a workstation browser; recipe-change
  windows are the natural rhythm. Schedule later only if it proves valuable
  (would need an in-cluster headless-browser path first).
- Structure: mirror improve-recipes exactly (SKILL.md plus one helper script),
  no Workflow machinery, no new infra. Parallelism via ad-hoc per-artifact
  subagents when a window is busy.

## Layout and data path

`.claude/skills/improve-artifacts/SKILL.md` plus
`.claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py`, invoked
like improve-recipes' helper: piped over stdin into the monolith pod's
`backend` container venv python. Subcommands:

- `gather --since <ts>`: list `<id>/index.html` objects in the artifacts bucket
  modified in the window, joined to `goosecracker_sessions` where possible for
  recipe and thread context. Skips ids whose `design-eval.json` already exists
  at the current `rubric_version`. Window default: the last `origin/main`
  commit touching `projects/firecracker/goosecracker/guest/recipes/`.
- `fetch-artifact <id>`: base64 of `index.html`.
- `put-eval <id> <b64-json>`: writes `<id>/design-eval.json` alongside the
  HTML. This is the only S3 write the skill may make. Payload passed as a
  base64 argv argument (stdin carries the script, same constraint as
  improve-recipes' put-eval).

## Screenshots

Local, on the workstation: decode the HTML to the scratchpad, serve the
directory with `python3 -m http.server`, then `npx playwright screenshot` at
1440x900 and 390x844, full page. One-time setup: `npx playwright install
chromium`. Documented caveat: local rendering lacks the production sandbox CSP,
so this is a design judge, not a security or embed-behavior check.

## Rubric v1

Fixed vocabulary so runs are comparable over time. `rubric_version: 1`; bumping
it is a PR to the SKILL.md, mirroring taxonomy versioning in improve-recipes.

Six dimensions scored 1-4, each with a one-line rationale:

1. typography
2. color
3. layout-rhythm
4. component-discipline
5. interaction-affordance
6. mobile-usability (judged from the 390px shot)

Plus two flag lists:

- slop-tells: the DESIGN BAR's own list (purple-blue gradients, gradient text,
  cyan/neon-on-dark, everything centered, nested cards, pure #000/#fff,
  default-stack fonts, lazy monospace).
- defects: visible breakage (overflow, dead-looking sections, unstyled
  components).

The rubric deliberately derives from the DESIGN BAR so scores measure
compliance with the bar the recipes already ship.

## design-eval.json contract

Written per artifact via `put-eval`, alongside the artifact's `index.html` in
`s3://artifacts/<id>/`. Keys:

- `artifact_id`
- `session_id` (nullable)
- `recipe` and recipe version
- `rubric_version`
- `scores`: dim to `{score, rationale}`
- `slop_tells`: list
- `defects`: list
- `viewports`: widths screenshotted
- `judged_at`: ISO timestamp, from `date -u`
- `recipes_ref`: git SHA of `origin/main` for the recipes dir at run time

Same role as improve-recipes' eval.json: makes generations comparable and feeds
the PR's before/after table.

## Judging

The operator (or per-artifact Opus subagents when the window is busy) reads
both screenshots plus the HTML (for token, type, and palette inspection) and
scores against the rubric. Synthesis stays in the main loop. Judgment quality
over wall time; parallelism is an execution detail, not machinery.

## Diagnose and ship

Aggregate the evals. Recurring low dimensions or repeated tells map to edits
in:

- `artifact-build.yaml` (DESIGN BAR prose),
- `artifact-review.yaml` (checklist), or
- the static `retry` gates in both files, when the defect is mechanically
  detectable.

Evidence rule, verbatim from improve-recipes: every diff hunk cites at least
one artifact id and what its screenshot showed. An edit that cannot cite a
specific artifact is dropped from the diff.

Gate edits must be tested locally before the PR: run the `node` check script
against fixture HTML that should pass and fixture HTML that should fail.

Normal worktree plus PR flow. PR body, in order:

1. Before/after rubric-score table across windows (median dimension scores,
   slop-tell histogram), computed from the design-eval.json files in each
   window.
2. Per-edit evidence rows: artifact id, scores, tells/defects, what the
   screenshot showed, and which diff line addresses it.
3. "Out of scope, observed" section for anything the evidence points at outside
   the two recipe files.

## Guardrails

- Postgres access is strictly read-only (SELECT only).
- S3 writes are limited to creating `design-eval.json`.
- Edits are limited to the two artifact recipe YAMLs.
- Fewer than about 5 artifacts in the window: report-only, no PR.
- Render failures not attributable to the recipe (CDN outage, https-blocked
  resource) are recorded as env-failure and never diffed.
- Every edited YAML must pass a `yaml.safe_load` sanity check.

## After merge

Recipes reach production through the existing pipeline (guest apko image
rebuild plus substrate chart bump). The next `/improve-artifacts` run's
before/after table is the verdict on whether a change worked.
