---
name: improve-recipes
invoke: explicit
summary: Explicit improve loop for recipes
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

# improve-recipes

On-demand feedback loop for goosecracker recipes. Gathers session outcomes from
Postgres and S3, classifies the worst ones against a fixed taxonomy, and drafts
a PR editing the responsible lever with evidence tied to specific sessions.

Design rationale (folded in): the loop optimizes two metrics, fast time to outcome
and minimum owner turns per interaction. The feedback signal is fully automatic
(objective run-ledger metrics plus an LLM classification of session transcripts, no
explicit rating step); it drafts concrete recipe edits and opens a PR with
session-tied evidence for Joe to merge, with CI plus review as the safety gate.

## How routing works now (read this first)

As of the runtime-recipe paradigm (the DeepSeek orchestrator constructs the recipe
at runtime; see ADR agents/036), routing is no longer done inside the guest. The
DeepSeek orchestrator constructs the recipe at runtime:

1. The orchestrator (ADR 036) emits a typed `submit_plan` tool call that SELECTS
   and SEQUENCES sub-recipes with per-step context. This is the routing brain.
2. Python (`projects/monolith/goosecracker/router_render.py`) renders that plan
   into a router recipe and injects it into the guest. Only the enabled
   sub-recipes are listed; the ordered steps replace classification.
3. The baked `projects/firecracker/goosecracker/guest/recipes/agent.yaml` is now
   only the FALLBACK router, reached when the orchestrator is unavailable, the
   plan is invalid, or the replan budget is exhausted.

So "bad routing" is almost never an `agent.yaml` problem anymore. The routing
levers are:

| Symptom                                                   | Lever                          | File                                                              |
| --------------------------------------------------------- | ------------------------------ | ----------------------------------------------------------------- |
| Wrong sub-recipe selected / bad sequence / mis-fit plan   | DeepSeek plan system prompt    | `projects/monolith/chat/orchestrator.py` (`plan_system_prompt()`) |
| DeepSeek misjudged a sub-recipe (its purpose was unclear) | Sub-recipe catalog description | `projects/monolith/goosecracker/recipe_catalog.py`                |
| A sub-recipe did the work badly                           | Sub-recipe body                | `projects/firecracker/goosecracker/guest/recipes/<id>.yaml`       |
| Generated router scaffolding is wrong (rare)              | Renderer                       | `projects/monolith/goosecracker/router_render.py`                 |
| Fallback router misbehaved (fallback path only)           | `agent.yaml`                   | `projects/firecracker/goosecracker/guest/recipes/agent.yaml`      |

## Goal metrics

Every change is judged against two numbers, not vibes:

1. Time to outcome: wall time from dispatch to a useful result.
2. Owner turns per interaction: every follow-up message means the first
   response missed. Fewer is better.

A third signal is now available per session (see `gather`): whether the run was
plan-driven (`orchestrator_route = goose`, with a `plan_step_count`) or fell
back to the baked agent (`failopen`). A rising fallback rate is itself a
regression to chase.

## Invocation

Optional argument: a lookback window, or one session_id for a targeted
"that just went badly" analysis.

Find the monolith pod. There is no pod named "backend": the API lives in the
`backend` CONTAINER of the `monolith-*` pod (alongside linkerd-proxy and
frontend). Verified live 2026-07-02; correct here if it drifts:

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
  < docs/runbooks/improve-recipes/scripts/improve_recipes_tool.py
```

`put-eval` cannot take both the script and the payload over stdin, so it
takes the eval JSON base64-encoded as an argv argument instead of stdin:

```bash
kubectl exec -i -n monolith <pod> -c backend -- env \
  PYTHONPATH=/projects/monolith/main.runfiles/_main/projects/monolith \
  /projects/monolith/main.runfiles/_main/projects/monolith/.main/bin/python3 - \
  put-eval <session_id> <base64-encoded-eval-json> \
  < docs/runbooks/improve-recipes/scripts/improve_recipes_tool.py
```

Default `--since` window: the merge date of the last commit touching ANY recipe
lever (guest recipes OR the monolith plan levers), whichever is later, so a
before/after window closes on the most recent recipe-affecting change:

```bash
git log -1 --format=%cI origin/main -- \
  projects/firecracker/goosecracker/guest/recipes/ \
  projects/monolith/goosecracker/recipe_catalog.py \
  projects/monolith/goosecracker/router_render.py \
  projects/monolith/chat/orchestrator.py
```

The previous generation's window is between the prior two such commits; use
it to compute the before/after aggregates for the PR body.

## Rank rules

Score completed sessions on `active_seconds` and owner_turns. Rank on
`active_seconds` (real guest work time, summed from sessions.db message gaps
with owner-reply pauses excluded), NOT `wall_seconds`: wall_seconds is the
whole thread lifetime and is dominated by time waiting for owner replies
between turns, so it overstates effort by 10x or more (a 2510s thread whose
guest actually ran ~152s). `wall_seconds` stays in the record for reference and
for spotting threads that sat idle a long time, but a slow, expensive session
is one with high `active_seconds`. `active_seconds` is null when the session
has no sessions.db (fall back to wall_seconds for those). Any session in a
FAILED state, or with a non-empty result_error, is automatically in the worst
set regardless of score. A session whose `orchestrator_route` is `failopen`
(fell back to the baked agent) is worth attention even if it completed: a
fallback that should have been a real plan is a routing miss. Deep-read the
worst 5 (or the single session the user named). Skip sessions where `has_eval`
is already true with the current taxonomy_version, they don't need
re-classifying.

## Deep-read

For each selected session:

1. `fetch-session <session_id>` to get the sessions.db blob, base64-encoded.
2. Decode it to a scratchpad file.
3. `sqlite3 <file> .schema` first, the goose session schema is not documented
   here on purpose: inspect it before assuming table names.
4. Extract the message and tool-call sequence from the schema you just found,
   and read it for the failure signal.
5. Cross-reference the `plan_json` and `plan_step_count` from `gather`: was the
   sequence DeepSeek chose right for what the transcript shows the task
   actually needed? A `replan` object in goose's final output (the run asked to
   be re-planned) is the strongest `plan-misfit` signal; it lives only in the
   session transcript, not in Postgres.

## Taxonomy v2

Fixed vocabulary so runs are comparable over time. `taxonomy_version: 2`.
Adding a mode means bumping the version in this file by PR.

Routing/planning modes (map to the DeepSeek plan levers, NOT `agent.yaml`):

- **wrong-route**: the plan selected the wrong sub-recipe for the task
- **bad-sequence**: the plan ordered steps wrongly, or omitted a step the task
  needed (multi-step runs)
- **plan-misfit**: the plan did not fit the real task and goose emitted a
  `replan` (or should have); the initial selection was off
- **unwarranted-fallback**: the run fell back to the baked agent
  (`orchestrator_route = failopen`) for a reason that is fixable in the plan
  levers rather than a genuine outage

Execution modes (map to the sub-recipe body that ran):

- **missing-context**: guest lacked information the recipe should have injected
  or asked for up front
- **tool-flail**: repeated failing tool calls or retries
- **over-clarification**: asked the owner something the recipe could have
  defaulted (direct hit on the minimum-turns goal)
- **under-delivery**: run completed but the owner had to follow up to get the
  real ask
- **structured-output-miss**: result JSON parse fell back to regex or narrative
- **env-failure**: infrastructure fault, out of recipe scope (reported, never
  diffed)

## eval.json contract

Written per session via `put-eval <session_id> <base64-payload>`, alongside the
session's sessions.db in `s3://artifacts/{session_id}/`. Required keys:

- `session_id`
- `recipe`
- `taxonomy_version`
- `failure_modes`: list of `{mode, confidence, rationale}`
- `metrics`: `{active_seconds, wall_seconds, owner_turns, tool_calls, retries}`
  (rank on `active_seconds`; keep `wall_seconds` for reference)
- `classified_at`: ISO timestamp, from `date -u`
- `recipes_ref`: git SHA of `origin/main` for the recipe levers at run time

For `taxonomy_version >= 2`, also record the plan signal so aggregates can track
routing quality and fallback rate:

- `plan`: `{orchestrator_route, plan_step_count, replan_observed}` (from
  `gather` plus the deep-read; `replan_observed` is a bool from the transcript)

Set a low-confidence flag on the classification when sessions.db was missing and
the classification came from `result_head` alone (see Guardrails).

## Diagnose and ship

Evidence rule: every diff hunk must cite at least one session_id. An edit that
cannot cite a specific session is dropped from the diff.

1. Map each confirmed failure mode to its lever via the table in "How routing
   works now". A routing/planning mode edits the DeepSeek plan system prompt or
   a catalog description; an execution mode edits the sub-recipe body. Do NOT
   edit `agent.yaml` routing criteria for a routing miss: that only changes the
   fallback path, not what actually ran.
2. Create a worktree and branch, following the repo's normal workflow.
3. Edit only the responsible lever files. A single PR may touch both a guest
   recipe and a monolith lever if the evidence supports each independently.
4. Sanity-check every edited file:
   - guest recipe YAML parses:
     `python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" <file>`
   - monolith python (`orchestrator.py`, `recipe_catalog.py`,
     `router_render.py`) compiles: `python3 -m py_compile <file>`
5. Bump the chart(s) for whatever you touched (see "After merge") in the same
   PR.
6. Open a PR. Body template, in order:
   - Before/after aggregates table: this generation vs the previous one (median
     wall time, mean owner turns, failure-mode histogram, AND fallback rate =
     failopen sessions / total), computed from the eval.json files in each
     window.
   - Per-edit evidence rows: session_id, metric values, failure mode,
     transcript excerpt, and which diff line addresses it.
   - "Out of scope, observed" section for anything the evidence points at
     outside the recipe levers (for example an fc-invoke timeout).

## Guardrails

- Postgres access is strictly read-only (SELECT only).
- S3 writes are limited to creating eval.json; the skill never touches
  sessions.db or any other object.
- Fewer than about 5 completed sessions in the window: report-only, no PR
  ("not enough evidence yet").
- env-failure is never diffed, it is infrastructure fault, out of recipe scope:
  report it, don't edit a recipe to work around it.

## After merge

The deploy path depends on which lever you edited:

- Guest sub-recipe bodies or the fallback `agent.yaml`
  (`projects/firecracker/goosecracker/guest/recipes/`): CI rebuilds the
  goosecracker guest apko image and bumps the substrate chart. Bump the
  substrate chart per its normal flow.
- Monolith plan levers (`chat/orchestrator.py` plan system prompt,
  `goosecracker/recipe_catalog.py`, `goosecracker/router_render.py`): these ship
  with the MONOLITH, not the guest image. Bump `projects/monolith/chart/Chart.yaml`
  and keep `projects/monolith/deploy/application.yaml` `targetRevision` in sync.
  No guest image roll happens for these, which is the point of the paradigm:
  routing changes deploy fast without a guest rebuild.

A PR that touches both levers needs both bumps. The next `/improve-recipes` run
is what tells you whether the change actually worked, that is the point of the
before/after aggregates table.

