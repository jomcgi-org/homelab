---
name: improve-recipes
description: >
  Classify goosecracker agent sessions from prod data and open evidence-backed
  PRs editing the recipe YAMLs. Use when asked to "improve the goose recipes",
  "/improve-recipes", "why did that agent session go badly", "classify agent
  sessions", or "recipe feedback loop".
---

# improve-recipes

On-demand feedback loop for goosecracker recipes. Gathers session outcomes
from Postgres and S3, classifies the worst ones against a fixed taxonomy, and
drafts a PR editing `projects/firecracker/goosecracker/guest/recipes/` with
evidence tied to specific sessions.

Design doc: `docs/plans/2026-07-01-improve-recipes-feedback-loop-design.md`.

## Goal metrics

Every recipe change is judged against two numbers, not vibes:

1. Time to outcome: wall time from dispatch to a useful result.
2. Owner turns per interaction: every follow-up message means the first
   response missed. Fewer is better.

## Invocation

Optional argument: a lookback window, or one session_id for a targeted
"that just went badly" analysis.

Find the backend pod (verify the namespace/label against the live cluster;
the command below is the last-known-good invocation, correct it here if it
drifts):

```bash
kubectl get pods -n monolith -o name | grep backend | head -1
```

Run a subcommand by piping the helper script over stdin into the pod's venv
python:

```bash
kubectl exec -i -n monolith <pod> -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  gather --since 2026-06-28T00:00:00Z \
  < .claude/skills/improve-recipes/scripts/improve_recipes_tool.py
```

`put-eval` cannot take both the script and the payload over stdin, so it
takes the eval JSON base64-encoded as an argv argument instead of stdin:

```bash
kubectl exec -i -n monolith <pod> -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  put-eval <session_id> <base64-encoded-eval-json> \
  < .claude/skills/improve-recipes/scripts/improve_recipes_tool.py
```

Default `--since` window: the merge date of the last commit touching
`projects/firecracker/goosecracker/guest/recipes/` on `origin/main`:

```bash
git log -1 --format=%cI origin/main -- projects/firecracker/goosecracker/guest/recipes/
```

The previous generation's window is between the prior two such commits; use
it to compute the before/after aggregates for the PR body.

## Rank rules

Score completed sessions on wall_seconds and owner_turns. Any session in a
FAILED state, or with a non-empty result_error, is automatically in the
worst set regardless of score. Deep-read the worst 5 (or the single session
the user named). Skip sessions where `has_eval` is already true with the
current taxonomy_version, they don't need re-classifying.

## Deep-read

For each selected session:

1. `fetch-session <session_id>` to get the sessions.db blob, base64-encoded.
2. Decode it to a scratchpad file.
3. `sqlite3 <file> .schema` first, the goose session schema is not
   documented here on purpose: inspect it before assuming table names.
4. Extract the message and tool-call sequence from the schema you just
   found, and read it for the failure signal.

## Taxonomy v1

Fixed vocabulary so runs are comparable over time. `taxonomy_version: 1`.
Adding a mode means bumping the version in this file by PR.

- **wrong-route**: agent.yaml dispatched the wrong sub-recipe
- **missing-context**: guest lacked information the recipe should have
  injected or asked for up front
- **tool-flail**: repeated failing tool calls or retries
- **over-clarification**: asked the owner something the recipe could have
  defaulted (direct hit on the minimum-turns goal)
- **under-delivery**: run completed but the owner had to follow up to get
  the real ask
- **structured-output-miss**: result JSON parse fell back to regex or
  narrative
- **env-failure**: infrastructure fault, out of recipe scope (reported,
  never diffed)

## eval.json contract

Written per session via `put-eval <session_id> <base64-payload>`, alongside
the session's sessions.db in `s3://artifacts/{session_id}/`. Keys:

- `session_id`
- `recipe`
- `taxonomy_version`
- `failure_modes`: list of `{mode, confidence, rationale}`
- `metrics`: `{wall_seconds, owner_turns, tool_calls, retries}`
- `classified_at`: ISO timestamp, from `date -u`
- `recipes_ref`: git SHA of `origin/main` for the recipes dir at run time

Set a low-confidence flag on the classification when sessions.db was missing
and the classification came from `result_head` alone (see Guardrails).

## Diagnose and ship

Recipe files live in `projects/firecracker/goosecracker/guest/recipes/`.

Evidence rule: every diff hunk must cite at least one session_id. An edit
that cannot cite a specific session is dropped from the diff.

1. Map each confirmed failure mode to the recipe section responsible
   (router criteria in agent.yaml, sub-recipe instructions, missing
   parameter, response schema gap).
2. Create a worktree and branch, following the repo's normal workflow.
3. Edit the recipe YAMLs only, nothing else.
4. Sanity-check every edited file parses:

   ```bash
   python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>
   ```

5. Open a PR. Body template, in order:
   - Before/after aggregates table: this generation vs the previous one
     (median wall time, mean owner turns, failure-mode histogram), computed
     from the eval.json files in each window.
   - Per-edit evidence rows: session_id, metric values, failure mode,
     transcript excerpt, and which diff line addresses it.
   - "Out of scope, observed" section for anything the evidence points at
     outside the recipes directory (for example an fc-invoke timeout).

## Guardrails

- Postgres access is strictly read-only (SELECT only).
- S3 writes are limited to creating eval.json; the skill never touches
  sessions.db or any other object.
- Fewer than about 5 completed sessions in the window: report-only, no PR
  ("not enough evidence yet").
- env-failure is never diffed, it is infrastructure fault, out of recipe
  scope: report it, don't edit a recipe to work around it.

## After merge

Recipes reach production through the existing pipeline: the merged PR's CI
rebuilds the goosecracker guest apko image and bumps the substrate chart, so
there is no extra deploy step. The next `/improve-recipes` run is what tells
you whether this change actually worked, that's the point of the before/after
aggregates table.
