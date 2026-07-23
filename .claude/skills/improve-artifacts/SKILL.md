---
name: improve-artifacts
description: >
  UI/UX quality feedback loop over published goosecracker artifacts: screenshot
  each artifact in the window, score it against a fixed design rubric, and open
  evidence-backed PRs editing the artifact recipe YAMLs. Use when asked to
  "improve the artifacts", "/improve-artifacts", "why do the artifacts look
  samey / like AI slop", or "design feedback loop". For mechanics failures
  (routing, turns, wall time) use improve-recipes instead.
---

# improve-artifacts

On-demand design-quality feedback loop for goosecracker artifact recipes.
Sister skill to improve-recipes, which selects on mechanics metrics (wall time,
owner turns) and is structurally blind to fast, no-follow-up sessions that
publish generic slop. This skill samples ALL artifacts published in the window,
judges them visually from screenshots, and edits only the two artifact recipe
YAMLs.

Design rationale (folded in): the artifact pipeline's three UI/UX layers (the
DESIGN BAR in `artifact-build.yaml`, the fresh-eyes pass in `artifact-review.yaml`,
and the static retry gates) are all feed-forward; nothing fed back from published
artifacts to the recipes. improve-recipes cannot fill the gap because it selects on
cost metrics and never renders the published HTML, so a fast, no-follow-up session
that ships slop never enters its worklist. This loop closes that gap by judging every
published artifact visually.

## Goal metric

Rubric compliance, not vibes: median dimension scores and the slop-tell
histogram across a recipe generation, compared before/after in every PR. The
rubric deliberately derives from the DESIGN BAR in artifact-build.yaml, so
scores measure compliance with the bar we already ship.

## Invocation

Optional argument: a lookback window, or one artifact_id for a targeted "that
looks bad" analysis.

Find the monolith pod (the API lives in the `backend` CONTAINER of the
`monolith-*` pod; there is no pod named "backend"):

```bash
kubectl get pods -n monolith -o name | grep '^pod/monolith-' | grep -v pg | grep -v atlas | grep -v searxng | head -1
```

Run a subcommand by piping the helper script over stdin into the pod's venv
python (note the `-c backend` container selector):

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  gather --since 2026-06-28T00:00:00Z \
  < .claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py
```

`put-eval` takes the eval JSON base64-encoded as an argv argument (stdin
carries the script):

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  put-eval <artifact_id> <base64-encoded-eval-json> \
  < .claude/skills/improve-artifacts/scripts/improve_artifacts_tool.py
```

Default `--since` window: the merge date of the last commit touching
`projects/firecracker/goosecracker/guest/recipes/` on `origin/main`:

```bash
git log -1 --format=%cI origin/main -- projects/firecracker/goosecracker/guest/recipes/
```

The previous generation's window is between the prior two such commits; use it
to compute the before/after aggregates for the PR body.

## Worklist

Judge EVERY artifact in the window (this is the point: slop does not rank on
mechanics metrics, so there is no "worst" pre-filter). Cap at 15, newest
first, if a window is unusually busy, and say so in the report. Skip artifacts
whose `has_design_eval` is true at the current rubric_version.

## Screenshots

Per artifact, on the workstation:

1. `fetch-artifact <artifact_id>`, decode to `<scratchpad>/<artifact_id>/index.html`.
2. Serve and shoot at desktop and mobile widths:

```bash
(cd <scratchpad>/<artifact_id> && python3 -m http.server 8931 &) && sleep 1
npx playwright screenshot --viewport-size "1440,900" --full-page \
  http://localhost:8931/index.html <artifact_id>-desktop.png
npx playwright screenshot --viewport-size "390,844" --full-page \
  http://localhost:8931/index.html <artifact_id>-mobile.png
kill %1
```

One-time setup if chromium is missing: `npx playwright install chromium`.

Caveat: local rendering lacks the production sandbox CSP, so this is a design
judge, not a security or embed-behavior check. If a page fails to render for
reasons outside the recipe's control (CDN outage, https-blocked resource),
record it as env-failure and never diff for it.

## Rubric v1

Fixed vocabulary so runs are comparable over time. `rubric_version: 1`.
Changing a dimension means bumping the version in this file by PR.

Six dimensions, each scored 1-4 with a one-line rationale:

1. **typography**: distinctive pairing, clear hierarchy, no default stacks
2. **color**: cohesive committed palette, tinted neutrals, readable contrast
3. **layout-rhythm**: varied spacing, asymmetry where it helps, no card-in-card
4. **component-discipline**: one token system (:root variables, single radius),
   not ad-hoc styling per element
5. **interaction-affordance**: visible hover/active/focus on every control
6. **mobile-usability**: judged from the 390px shot; no overflow, tap targets
   usable, text wraps

Two flag lists:

- **slop_tells**: purple-blue gradients, gradient text on headings or numbers,
  cyan-on-dark, neon-on-dark, everything centered, cards nested in cards, pure
  #000 or #fff, default-stack fonts (Inter/Roboto/Arial/system), monospace as
  lazy "techie" shorthand
- **defects**: visible breakage (overflow, dead-looking or unstyled sections,
  missing icons, half-rendered components)

Score from BOTH screenshots plus the HTML itself (token discipline, type
stack, palette live in the code).

## design-eval.json contract

Written per artifact via `put-eval <artifact_id> <base64-payload>`, alongside
the artifact's index.html in `s3://artifacts/{artifact_id}/`. Keys:

- `artifact_id`
- `session_id` (the Discord thread id, from gather; nullable if no session row matched)
- `recipe` (from gather; nullable)
- `rubric_version`
- `scores`: dimension -> `{score, rationale}`
- `slop_tells`: list of strings from the fixed vocabulary
- `defects`: list of strings
- `viewports`: widths screenshotted, e.g. `[1440, 390]`
- `judged_at`: ISO timestamp, from `date -u`
- `recipes_ref`: git SHA of `origin/main` for the recipes dir at run time

## Judging

Judge each artifact yourself, or when the window is busy fan out per-artifact
subagents that each Read the two PNGs plus the HTML and return the eval JSON;
synthesis stays in the main loop. Judgment quality over wall time.

## Diagnose and ship

Recipe files live in `projects/firecracker/goosecracker/guest/recipes/`. The
editable surface is exactly two files: `artifact-build.yaml` and
`artifact-review.yaml`, prose AND static retry gates.

Evidence rule: every diff hunk must cite at least one artifact_id and what its
screenshot showed. An edit that cannot cite a specific artifact is dropped.

1. Aggregate the evals: recurring low dimensions or repeated tells map to the
   DESIGN BAR prose (artifact-build.yaml), the review checklist
   (artifact-review.yaml), or, when the defect is mechanically detectable, a
   new static check in BOTH files' `retry` gates (the slate-705 path: a
   recurring visual defect promoted to a mechanical gate is this loop's
   highest-leverage output).
2. Gate edits must be tested locally before the PR: run the node check script
   against a fixture HTML that should pass and one that should fail, and show
   both results.
3. Create a worktree and branch, following the repo's normal workflow.
4. Edit the two recipe YAMLs only, nothing else. Sanity-check every edited
   file parses:

   ```bash
   python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>
   ```

5. Open a PR. Body template, in order:
   - Before/after rubric table: this generation vs the previous one (median
     score per dimension, slop-tell histogram), computed from the
     design-eval.json files in each window.
   - Per-edit evidence rows: artifact_id, scores, tells/defects, what the
     screenshot showed, and which diff line addresses it.
   - "Out of scope, observed" section for anything the evidence points at
     outside the two recipe files.

## Guardrails

- Postgres access is strictly read-only (SELECT only).
- S3 writes are limited to creating design-eval.json; never touch index.html,
  sessions.db, eval.json, or any other object.
- Fewer than about 5 artifacts in the window: report-only, no PR ("not enough
  evidence yet").
- env-failure (render failure not attributable to the recipe) is reported,
  never diffed.
- Edits limited to artifact-build.yaml and artifact-review.yaml.

## After merge

Recipes reach production through the existing pipeline: the merged PR's CI
rebuilds the goosecracker guest apko image and bumps the substrate chart. The
next `/improve-artifacts` run's before/after table is the verdict on whether
the change worked.
