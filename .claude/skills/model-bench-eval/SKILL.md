---
name: model-bench-eval
description: >
  Run the model-bench LLM leaderboard eval and publish the results. Use when asked to
  "run the evals", "run model-bench", "re-bench the models", "update the leaderboard",
  or after changing tasks/models in projects/model-bench. Covers the two-provider split
  (billed OpenRouter candidates vs free Claude-Code anchors + judge), fixture snapshotting,
  the paid-run handoff, and committing the regenerated leaderboard to a PR.
---

# Running the model-bench eval

The bench lives in `projects/model-bench/` and answers: **which model should I run for
homelab workloads?** It is a **local, manual, partly-billed** tool, not CI. Read
`projects/model-bench/README.md` for the data model; this skill is the run procedure.

## The cost model (read first)

There are two providers, and only one of them costs money:

- **Candidates** (`provider: openrouter`, `role: candidate`) are the self-hostable /
  cheap-cloud models you might actually deploy. They are rented per-token via OpenRouter
  and **billed** (cents to low dollars per agentic task). Their cost/turns/tokens are the
  whole point of measuring them.
- **The Claude ceiling** (`provider: claude-code`, `role: anchor`) and the **judge** run
  through the local `claude` CLI under the Max subscription: **free**. Anchors are a
  capability ceiling (pass + wall-time), not a cost-ranked competitor.

So a full run only bills for the candidate columns. **Never** re-add Claude/Gemini as paid
OpenRouter rows to "measure their cost" — that is spend on a number that has no bearing on
actual usage (Claude is consumed via the flat subscription). If a run looks expensive,
check `models.yaml` for a pricey cloud model that crept back to `status: active`.

## Preconditions

Verify before running (do not assume):

1. **Verifier venv** exists: `~/.cache/model-bench-venv/bin/pytest` (resolved via
   `$MODEL_BENCH_VENV`). If missing, create it per the README setup block.
2. **`claude` CLI** on PATH (for anchors + judge). `claude --version` should work.
3. **`OPENROUTER_API_KEY`** — only needed if any candidate (`provider: openrouter`) is in
   the run. An anchors-only run does not need it. The key is billed, so **you (the model)
   do not hold it**: the human runs the billed step via `!` in the session.
4. **Fixtures materialized**: snapshot tasks (`snapshot:` block in `task.yaml`) are
   gitignored and must be regenerated. Run `python3 -m bench snapshot` (all) or
   `python3 -m bench snapshot <task-id>`.

## Procedure

Run everything from `projects/model-bench/`.

1. **Snapshot fixtures** (regenerates the gitignored whole-monolith trees from their
   pinned commits; deterministic, byte-stable):
   ```bash
   python3 -m bench snapshot
   ```
2. **Run the billed candidate columns.** This spends real money, so the human runs it via
   `!` (their shell holds `OPENROUTER_API_KEY`; it never passes through the model):
   ```bash
   ! cd projects/model-bench && python3 -m bench run
   ```
   Scope it while iterating on one task/model to keep cost down:
   `python3 -m bench run --task <id> --model <substring>`. Results cache in
   `~/.cache/model-bench/results` (outside the worktree, survive `worktree remove`),
   keyed on prompt+fixture+verifier+model+budget, so unchanged cells are skipped and only
   new work bills.
3. **Run the free anchor + judge columns.** These need no key, so the model can run them
   directly:
   ```bash
   python3 -m bench run --model claude
   ```
   (`--model claude` filters to the `provider: claude-code` anchors; the judge always runs
   via the `claude` CLI regardless of candidate.)
4. **Regenerate the report** and the public page JSON:
   ```bash
   python3 -m bench report
   ```
   Inspect `reports/leaderboard.md`: the candidate tables (cost-ranked) and the separate
   **Frontier ceiling** section (anchors, pass + wall-time). Sanity-check that the new/
   changed task actually spreads the models rather than saturating at 1.0.
5. **Commit the deliverable to the PR.** Only `reports/leaderboard.md` and the page
   `leaderboard.json` are version-controlled (the per-cell JSON is not). Commit those on
   the feature branch; do not commit fixtures or `results/`.

## Invalidation

Bumping a task prompt, a verifier, a fixture commit, or an agent budget changes the cache
key and re-runs the affected cells. A harness-behaviour change bumps `HARNESS_VERSION`
(in `bench/cache.py`), which invalidates **every** cell — a full paid re-run. When you are
about to invalidate broadly, fold the model-set changes in first and re-bench once on the
final set, rather than paying to bench the old set and then the new one.

## Do not

- Run `bazel test` / `pytest` for the bench from a workstation as an inner loop (no darwin
  CI runners). Byte-compile changed files and lean on CI. The bench's own `bench run` is
  the exception: it is designed to run locally.
- Add `@sha256` digests, Dockerfiles, or paid Claude rows. See repo `CLAUDE.md`.
