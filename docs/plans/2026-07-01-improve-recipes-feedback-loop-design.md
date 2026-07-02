# Design: /improve-recipes, a feedback loop for goosecracker recipes

Date: 2026-07-01
Status: Approved (brainstorming session with Joe)

## Goal

Classify goosecracker agent interactions after the fact and iteratively improve the
recipe YAMLs, optimizing for two metrics:

1. Fast time to outcome (wall time from dispatch to a useful result)
2. Minimum owner turns per interaction (every follow-up message means the first
   response missed)

## Decisions made during brainstorming

- Feedback signal: fully automatic. Objective metrics from the existing run ledger
  plus an LLM classification of session transcripts. No explicit rating step for Joe.
- Apply mode: the loop drafts concrete recipe edits and opens a PR with the evidence;
  Joe merges. CI plus review are the safety gate.
- Trigger: on-demand skill only (no scheduled infra). Invoked as /improve-recipes in a
  Claude Code session.
- Change scope: recipe files only, under
  projects/firecracker/goosecracker/guest/recipes/. Anything else the evidence points
  at becomes a reported finding in the PR body, not a diff.
- Approach: read-only skill (Option A) plus a per-session classification file in S3
  (Joe's addition), rather than a Postgres eval ledger or an eval-gated replay
  harness. Replay evals through fc-invoke are the phase-2 door, opened once merged
  recipe PRs give us a set of real regression tasks.

## Existing data trail (no instrumentation changes needed)

| Signal                   | Where                                   | What it gives us                                                                           |
| ------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------ |
| Run ledger               | claude_agent.agent_threads              | recipe, tier, task, state, result JSON, result_error, created_at, completed_at (wall time) |
| Owner conversation       | chat.goosecracker_sessions.transcript   | accumulated owner instructions; follow-up count is the turns-per-interaction metric        |
| Full internal transcript | s3://artifacts/{session_id}/sessions.db | every guest message and tool call; internal flailing, retries, clarification requests      |
| Recipe generations       | git log on the recipes dir              | merge dates of recipe changes bucket sessions into before/after cohorts                    |

Recipes are baked into the guest apko image and rolled by a substrate chart bump, so
a merged recipe PR reaches production through the existing pipeline with no extra
deploy step in this design.

## Skill flow (single invocation)

The skill lives at .claude/skills/improve-recipes/SKILL.md in this repo. Optional
argument: a lookback window (default: sessions since the last merged recipe change,
so each run evaluates the current recipe generation) or one session_id for a
targeted "that just went badly" analysis.

1. Gather: read-only psql (in-pod venv recipe) over agent_threads joined to
   goosecracker_sessions for the window: recipe, task, state, result, result_error,
   wall time, owner follow-up count parsed from transcript.
2. Rank: score each session on the two goal metrics plus hard failures (result_error,
   FAILED state). Select the worst N (default 5) plus any explicitly named session.
3. Deep-read and classify: fetch sessions.db for the selected sessions, extract the
   internal message and tool-call sequence, classify into the fixed taxonomy below,
   and write eval.json back to the session's S3 folder.
4. Diagnose and edit: map each confirmed failure mode to the recipe section
   responsible (router criteria in agent.yaml, sub-recipe instructions, missing
   parameter, response schema gap). Draft the minimal YAML edits.
5. Ship: worktree branch, edit the recipe YAMLs only, open a PR whose body contains
   the evidence table (session id, metric values, failure mode, transcript excerpt,
   and which diff line addresses it). Out-of-scope observations (for example an
   fc-invoke timeout) go in an "Out of scope, observed" section of the PR body.

## Per-session eval.json in S3

Written to s3://artifacts/{session_id}/eval.json alongside sessions.db as sessions
are classified. Purpose: persistence and incrementality without a database migration,
colocated with the data it describes.

- Contents: session_id, recipe, taxonomy_version, failure modes with confidence and
  a one-line rationale, metric values (wall time, owner turns, tool-call count,
  retry count), classified_at, and the git ref of the recipes dir at run time.
- Incremental runs skip sessions that already have an eval.json with the current
  taxonomy_version; bumping the taxonomy version forces re-classification.
- Window-level aggregates (section below) are computed by listing eval files rather
  than re-reading transcripts.

## Classification taxonomy (versioned with the skill)

Fixed vocabulary so runs are comparable over time. New modes are added by PR.

- wrong-route: agent.yaml dispatched the wrong sub-recipe
- missing-context: guest lacked information the recipe should have injected or asked
  for up front
- tool-flail: repeated failing tool calls or retries
- over-clarification: asked the owner something the recipe could have defaulted
  (direct hit on the minimum-turns goal)
- under-delivery: run completed but the owner had to follow up to get the real ask
- structured-output-miss: result JSON parse fell back to regex or narrative
- env-failure: infrastructure fault, out of recipe scope (reported, never diffed)

## Measuring whether the last change worked

Each run computes window-level aggregates (median wall time, mean owner turns,
failure-mode histogram) for the current recipe generation and for the previous one
(sessions between the prior two recipe merges, found from git log on the recipes
dir). The before/after table leads the PR body, so every recipe PR states whether
the previous one improved things.

## Guardrails and error handling

- Postgres access is strictly read-only. S3 writes are limited to creating eval.json;
  the skill never touches sessions.db or any other object.
- Missing sessions.db (pre-persistence sessions): classify from agent_threads.result
  alone and flag the classification lower-confidence in eval.json.
- Fewer than about 5 completed sessions in the window: report-only, no PR ("not
  enough evidence yet").
- Evidence rule: a proposed edit that cannot cite at least one specific session is
  dropped from the diff.
- Recipe YAMLs are parsed and schema-sanity-checked after editing, before the PR
  opens.

## Out of scope (deliberately)

- Scheduled or continuous classification (revisit if session volume grows)
- A Postgres eval ledger (eval.json in S3 covers persistence at current scale)
- Golden-task replay through fc-invoke before merging (phase 2, once real
  regressions exist to curate from)
- Changes outside the recipes directory
