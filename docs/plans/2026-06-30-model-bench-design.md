# model-bench: a repo-specific, cost-aware model qualification benchmark

Date: 2026-06-30
Status: Design approved, pending implementation plan
Branch: `feat/model-bench`

## Purpose

Find a **budget tier** of OpenRouter models that meet this repo's real requirements,
so we can route offloadable work to the cheapest model that clears the bar instead of
defaulting everything to Opus/Sonnet.

This is a **qualification screen**, not a leaderboard. The decision it feeds is coarse:
"which cheap model can I safely route this class of task to?" The deliverable is a
shortlist of budget models that clear our bar per offloadable task class, ranked by
cheapness, with our currently-trusted models as calibration anchors.

The instrument encodes the objective function already written in `CLAUDE.md`'s
model-routing section: route work to the cheapest model whose mistakes are caught
cheaply. Sonnet handles the locally-verifiable implementation bulk; Opus is reserved for
judgment and CI-only-verifiable work. model-bench discovers which cheaper OpenRouter
models are Sonnet-substitutable on the classes we would actually offload.

## What "the work" is (grounded in commit history)

A review of the last 300-500 commits shows the shape the task pack must mirror:

- Config-heavy and multi-language: YAML (Helm/ArgoCD) dominates changes, ahead of Go,
  then Python, with some JS/Svelte, Starlark/Bazel, and SQL.
- Small, surgical diffs: p50 = 2 files changed, p90 = 5. Real tasks are focused edits.
- Bot noise is large: 247 of 275 recent `chore` commits are chart-version-bot bumps.
  The real human/agent work is `feat` / `fix` / `perf` / `refactor`.
- The hard parts are exactly what the routing doc names as CI-only-verifiable: Helm
  value plumbing several levels deep, RBAC verb coverage, cross-service URL wiring,
  Bazel/apko, migration ordering. First-pass-correctness on these is the outcome we pay
  the quality tier for.

## Core design decisions

### Grading: hybrid, deterministic-first

- **Deterministic vendored-tool verifiers are the headline score.** `helm template`
  renders and a specific rendered field is asserted; `go build` / `python -c` compiles;
  `ruff` / `buildifier` clean; RBAC coverage asserted by parsing the handler's actual
  `(verb, resource)` calls and checking the ClusterRole covers each. These are the same
  vendored tools CLAUDE.md calls "tools to validate locally" and do not touch Bazel/RBE,
  so the loop runs fast on a workstation despite the repo's "no local test loop" rule
  (that rule is specifically about `bazel test` on unprovisioned darwin runners).
- **LLM-as-judge is a narrow lane, free-text only** (commit-message quality, no-em-dash
  style) where no mechanical check exists. Never used for anything a deterministic
  verifier can grade.

### I/O contract: full-file replacement

The model emits the complete new content of each target file; the harness writes it into
the fixture tree and runs the verifier. Structured JSON answers for naturally-targeted
tasks (e.g. "which verbs does this endpoint need"). Robust/lenient output extraction so
we measure capability, not envelope-format compliance (OpenRouter providers vary here).
Chosen over unified-diff to avoid the patch-apply confound (malformed patch scoring as a
wrong answer), which biases by model.

### 2-shot with tool feedback

- Shot 1 -> verifier -> `pass@1` recorded with latency + tokens.
- On fail, the verifier's **real stderr** (never the golden answer) is fed into shot 2.
- Shot 2 pass -> `pass@2`, recorded with cumulative latency + tokens as the markdown.
- Still wrong -> `fail`.

This models the repo's actual objective: first-pass-correctness is the premium (pass@1),
a second validator round still ships a correct outcome but costs latency + tokens (the
markdown), and outright failure is the floor. It maps onto the recognized self-repair /
self-debug eval mode. `pass@1` (greedy) and `pass@2` (feedback-repair) are always
reported as **distinct** columns, never blended into one "quality" number; the retry
advantage is known to be model-dependent (favors strong models that comprehend errors),
which is itself signal for budget selection.

### Task classes and the offload map

Each task carries a `class`:

- `mechanical` (chart-version bump dual-file sync, format)
- `config-plumbing` (deep Helm values, cross-service `.svc` URL wiring)
- `code-fix` (Go / Python)
- `free-text` (commit message, style) -- the only judge-graded class

The report answers qualification **per class**, so a model can qualify for the
safe-to-offload classes (`mechanical`, `config-plumbing`) even if it flunks the harder
ones. That is exactly the routing decision we would make by hand.

### Anchors and the qualification bar

We run currently-trusted models (Opus 4.8, Sonnet 4.6, both on OpenRouter) in the same
pack as **calibration anchors**. A budget model **qualifies** for a class if its `pass@1`
on that class is at least the anchor's (default anchor: Sonnet), at materially lower cost.
Relative-to-anchor bar chosen over a fixed absolute threshold because it matches the
routing doc's "as good as what I'd otherwise use."

### Scoring / report: coarse Pareto tiers

Persist per cell: `pass@1`, `pass@2`, `fail`, latency (p50/p95), prompt/completion
tokens, and `$` (OpenRouter usage x `/models` pricing). The leaderboard computes the
Pareto frontier on **(pass@1 rate, $cost)** plus a second frontier on
**(pass@1 rate, median latency)**, dominated active models flagged. Read as **coarse
tiers** (one-shots / needs-repair / can't), not a fine ranking -- ties are ties. This is
field-standard (Aider's leaderboard reports a cost column against pass rate).

Quality axis = `pass@1` rate (first-pass-correctness is the premium we actually pay for);
`pass@2` recovery is a secondary column, not folded into the frontier.

### Model registry with retirement

`models.yaml` is a registry, not a flat list. Each entry has
`status: active | experimental | retired`. The runner evaluates `active` (plus
`experimental` if opted in). `bench drop <model> --reason "..."` retires a model:
its committed result cells stay, it drops out of the active frontier, and it appears in a
separate **Retired tombstone table** (final pass@1, cost, reason, date). A
`bench prune --retired` reclaims space later, keeping the tombstone. The tombstone
preserves the institutional memory of _why_ an experiment failed, so a later re-add does
not silently re-run a known loser -- the same reasoning ADRs apply to rejected options.

### Caching / invalidation

Cell cache key = `sha256(prompt + fixture + verifier + harness_version + model_id +
params)`; the short hash is in the result filename. `run` skips cells whose file exists
(free rerun); editing any task input auto-stales only that task's cells across all models;
`--force` overrides. A human-readable `version: vN` tag is metadata + drift warning (warns
if content changed but the tag did not). **The verifier version is part of the key**, so
strengthening a verifier invalidates and recomputes affected cells -- essential because
grader strength changes results. Each cell records full provenance (model id/params,
harness version, prompt-template hash, timestamp).

### Determinism

Every model runs at **temperature 0** (greedy), one run per cell. This removes almost all
run-to-run variance (making single runs and the cache meaningful) and gives cross-model
parity, at a fraction of the cost of repeats. No N-repeats, no confidence intervals -- the
coarse-tier reading absorbs a stray provider-side flip.

### Safety: sandboxed verifiers

Verifiers execute model-generated content (`helm template`, compilers, greps) and feed
real stderr back, so model output is untrusted input. Each verifier runs in a scrubbed
subprocess env with **no cluster credentials** (`KUBECONFIG` unset), no 1Password /
OpenRouter tokens, a temp cwd, resource limits, and network off where the OS allows. The
homelab-specific teeth: the benchmark must never be able to touch the live GitOps cluster
via a model-emitted values file. macOS caveat: full network isolation is awkward locally;
env-scrubbing + temp-dir + no-creds is the portable floor, tightened if run in CI.

## Layout

```
projects/model-bench/
  models.yaml                # registry: OpenRouter model ids, status, params
  bench/                     # harness: cli, runner (2-shot), grading, cache, pareto, schema, sandbox
  tasks/<task-id>/
    task.yaml                # prompt, class, version tag, target files, verifier spec
    fixture/                 # frozen input tree (files at the commit's parent state)
    verifier.{py,yaml}       # effect-asserting deterministic check; emits (passed, real-tool-stderr)
    expected/                # golden reference for fuzzy / structured / judge grading
  results/<model>/<task>/<contenthash>.json   # one committed cell per task x model x hash
  reports/leaderboard.md     # generated coarse-tier Pareto view + retired tombstone table
```

Runtime: Python CLI run via the vendored `python` (async `httpx` for fan-out across
models x tasks; `pydantic` schemas). OpenRouter is HTTP+JSON, no SDK lock-in.
`OPENROUTER_API_KEY` from env.

## Seed task pack (~10-20, distilled from real commits)

Mirrors the commit-history distribution and the routing doc's CI-only-verifiable hot
spots. Each seed maps to a real merged commit and an effect-asserting verifier:

- Helm values plumbing several levels deep (`helm template` renders + assert a deep key)
- RBAC verb coverage for a new endpoint (parse handler calls, assert ClusterRole covers)
- Chart bump dual-file sync (`Chart.yaml` version <-> `application.yaml` targetRevision)
- Cross-service `.svc.cluster.local` URL wiring (the no-hardcoded-default trap; semgrep-checkable)
- Bazel / apko config (buildifier-clean, dual-arch)
- A Go fix and a Python fix (compile + targeted effect assertion)
- Conventional-commit message + no-em-dash style (the LLM-judge free-text lane)

Each task's verifier is hand-audited (SWE-bench Verified's quality spirit) to be neither
under-specified nor over-strict.

## Research grounding (deep-research, 2026-06-30)

A cited, adversarially-verified research pass (110 agents, 27 sources, 22/25 claims
confirmed) endorsed the spine and sharpened the weak spots. Adopted from it:

1. **Execution-based verifiers as headline** -- field-standard since HumanEval. Confirmed.
2. **Verifiers must assert on effects, not exit codes** -- on SWE-bench, weak graders let
   ~8-20% of "solved" patches be wrong, and strengthening the grader moved the top agent
   rank 1 -> 5. Drives the effect-asserting verifier requirement above.
3. **Sandbox model-generated shell** -- untrusted input; drives the no-credentials sandbox.
4. **Harden the tiny judge lane** -- rubric position/criterion-order bias flips top-1 on
   16-39% of prompts and judge rationales do not self-reveal bias; permute order, strip
   cues, never self-judge, spot-check against human labels.
5. **Self-repair is a recognized mode** (Self-Debugging +up to 12%; two rounds capture
   76-95% of gains) -- validates the 2-shot budget; charge it on cost, report pass@1 and
   pass@2 distinctly.
6. **Version the verifier + per-cell provenance** -- since grader strength changes results.
7. **Measure capability not format** -- lenient output extraction, fixed prompt template
   with parity, disclosed as a confound.
8. **Pareto-with-cost is field-standard** (Aider leaderboard cost column). Confirmed.

### Deliberate overrides (goal-driven, recorded on purpose)

Two research recommendations are knowingly declined because this is an internal,
repo-specific model-selection tool, not a public capability leaderboard:

- **No decontamination program** (research called instance mutation "mandatory"). Our
  benchmark's target _is_ this repo, so there is no generalization gap to protect and
  overfitting-to-repo is the desired outcome. Authoring our own prompts/verifiers and
  grading on effects already captures most of the anti-memorization benefit incidentally.
  The residual risk (a weak model passing a benchmarked task by recall yet fumbling the
  next novel one) is accepted: at homelab scale it is cheap and self-correcting, and the
  pack self-refreshes as new commits land.
- **No N-repeats / confidence intervals** (research flagged 10-20 tasks as fragile for
  _fine_ ranking). We only need coarse tiers; temperature 0 removes the run-to-run
  variance those intervals would capture. The one thing kept from that area is the task
  _quality_ audit, not quantity.

## Out of scope (v1, YAGNI)

- Auto-miner that generates tasks from git history (curated pack is what the verifiers need).
- Remote / CI verifiers (local vendored tools are enough).
- Web UI.
- Prompt-format robustness testing via paraphrases (template held constant + disclosed instead).

## Open follow-ups

- If coarse tiers ever prove unstable in practice, measure our own run-to-run variance
  with a small pilot before adding repeats (do not add CIs speculatively).
- The pack self-refreshes: fold in new tasks from notable commits over time.
