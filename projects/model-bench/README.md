# model-bench

model-bench is an internal Python CLI that screens OpenRouter LLM models against a curated pack of coding and config tasks drawn from this repo's real commits, to identify budget-tier models that clear the quality bar for offloadable work.

## Two run modes

- **single-shot** (`mode: single-shot`, the default): the model emits a whole file or
  answer in one turn; a deterministic verifier or an LLM judge grades it.
- **agentic** (`mode: agentic`): the model is dropped into a real repo snapshot with
  file tools (`list_dir`/`read_file`/`write_file`/`done`) and edits the code itself over
  several turns. This is the primary contract: native tool-calling carries file content
  in API-serialized JSON, so the output-format noise that dominated single-shot is gone,
  and it measures agentic reliability and token/turn efficiency alongside raw capability.

## SWE-bench-style real-monolith tasks

An agentic task graded by the repo's own tests works like SWE-bench:

1. `snapshot:` pins the **parent** of a real fix commit and lists the monolith paths to
   materialize into `fixture/` (`bench snapshot` uses `git archive`, so fixtures are real
   repo state and re-generate deterministically).
2. The model explores that fixture and makes the change the prompt describes.
3. The `pytest` verifier drops the **gold test** from the fix commit onto the workdir
   (a hidden grader the model never sees) and runs it on the monolith venv. On the buggy
   snapshot it fails; a correct edit makes it pass (fail-to-pass).

For example, `hikes-walkhighlands-dom-01` and `hikes-walkhighlands-duration-01` are both
agentic tasks against the hikes doability model (DOM scraping and duration-aware doability,
respectively). `tasks/` is the source of truth for the full, current list of agentic and
single-shot tasks.

## Graded (mutation-testing) tasks

Most verifiers are pass/fail. A graded verifier also records a 0..1 `score` on the
attempt, so a partial answer shows up as partial rather than as a plain fail. The
`mutation` verifier is the first:

1. The model writes a pytest suite for a real module (e.g.
   `whatsapp-timeparse-mutation-01` tests `chat/whatsapp_timeparse.py`).
2. The suite must pass on the unmodified module, which is pinned by sha256 so the
   model cannot edit it.
3. Each hidden mutant in task.yaml (one find/replace modelled on a plausible bug) is
   applied to a fresh copy, and the suite is re-run. The score is the fraction of
   mutants the suite catches; `passed` is `score >= pass_threshold`.
4. The `equivalent` mutants are behaviour-preserving rewrites. A suite that fails on
   one is asserting on source text rather than behaviour, and scores 0.

A mutant's `find` text must occur exactly once in the module, so bumping the fixture
commit fails loudly instead of silently grading a no-op.

The leaderboard shows the mean graded score over scored tasks next to the hard
pass count. A task is scored when any current, non-retired cell on it carries a
graded score. Failed cells without a score count as 0; harness errors are excluded.

## Performance (speedup) tasks

The `speedup` verifier grades a performance change. task.yaml carries the original
module (`baseline`) and a hidden harness script. The harness first checks that the
candidate's output equals the original's, on edge cases and on the benchmark inputs,
then times both in one process, interleaved, on fresh inputs per pair so caching
across calls cannot help. Any output difference scores 0. Wall-time ratios are noisy,
so the score is bucketed (`buckets: [[min_speedup, score], ...]`) with gaps wide
enough that run-to-run noise does not flip a bucket. `stars-grid-speedup-01` asks
for a faster point-in-polygon grid generator: micro-optimisation stays in the bottom
bucket, a per-row scanline reaches 0.75, and an edge-bucket scanline reaches 1.0.


The `checks` verifier is the general form: a hidden task-authored script runs with
the workdir as cwd and prints `{"checks": {name: bool | 0..1}}`, and the score is
the weighted mean (`weights`, default 1 each). Scripts that render charts get the
Helm binary as `$HELM`, resolved from `args.helm`, then `$MODEL_BENCH_HELM`, then
`helm` on PATH. A snapshot can add `overlays: [{commit, paths}]` to lay files from a
second commit over the first (same strip), e.g. a chart at a fix's parent with the
app entrypoint from the fix itself.


The `pytest` verifier has a graded multi-site mode too. When a real fix had to touch
several places, `sites:` maps each site to the gold test targets that prove it fixed,
and each site runs as its own pytest invocation. The score is the fraction of sites
fixed, so a model that repairs only the reported symptom lands at a partial score.
`trace-correlation-multisite-01` is the first such task: its prompt reports one
site from the API pod and states the rest as a property of every span-exporting
process, and there are three sites to find.


## Seeded code-review tasks

A `review-findings` task (e.g. `chat-public-retention-review-01`) snapshots a real
feature commit, plants bugs into it with `snapshot.patches` (find/replace edits kept
in task.yaml, so the fixture still regenerates from git), and writes the change as
`REVIEW.diff` via `snapshot.review_diff` (base commit to the patched tree). The model
writes `review.json` as `[{file, line, description}]`. A finding matches a planted
bug on the same file within the bug's line range plus `tolerance`. The score is
`(matched - fp_penalty * false_positives - decoy_penalty * decoy_hits) / bugs`,
floored at 0, so spraying findings scores nothing. `decoys` list code that looks
wrong but is correct in context; a finding is owned by the nearest bug or decoy,
and a bug's `lines` may list several ranges when the defect shows in more than one
place. A patch with `content` instead of `find`/`replace` writes a whole file (a
rewritten module, or a new migration). `-02` is the harder sibling of `-01`: ten
cross-referencing plants and four decoys.


## Norms score (quality above the pass floor)

A passing agentic cell also gets a deterministic `norms` record (`bench/norms.py`),
computed from the fixture and the authored tree before the verifier writes hidden
tests or runner artifacts: files changed outside `target_files`,
leftover debug lines (`print(` outside tests, `breakpoint(`, `console.log(`, TODO /
FIXME / XXX), new ruff or golangci-lint findings (installed tools at pinned versions),
diff size against `gold_diff_lines`, whether code changed without a test, and added
comment density compared with the original changed files. Comment density covers
Python, Go, JavaScript and TypeScript code, excludes tests and string literals,
and tolerates an absolute density change of 0.10 before penalising it.
`python -m bench gold-size --repo ../.. --write` derives `gold_diff_lines` from
the real `source_commit`, restricted to the snapshot's paths, stripping and
exclusions. It leaves the size unset when the pre-fix tree cannot be projected
or the source commit has no text fix in those paths. Only the integer is stored.
`norms_score`
is 1 minus a weighted mean of those penalties (weights in `WEIGHTS`); a signal the task
cannot measure is N/A and the rest are renormalised. Lint runs without downloads
on isolated copies or stdin; missing tools, version mismatches and analysis errors
are N/A. Version 2 records include `norms_version`; older records remain readable
without rerunning billed cells. The leaderboard shows the mean over scored passing
cells with its `norms_n` coverage as the `norms` column. These signals do not change
leaderboard ordering or pass qualification.

## Pairwise judge (quality above the pass floor)

`python -m bench judge` ranks what the verifier and the deterministic norms cannot:
given two passing changes for the same task, which would a careful reviewer of this
repo rather merge? Passing agentic cells store their change as a capped unified diff
(`ResultCell.diff`), so judging runs any time after the bench run.

- Blind and position-debiased: the judge sees changes A and B, never model ids, and
  every pair is judged in both orders. Only a verdict that agrees across both orders
  counts; a flip is a tie.
- No self-judging: the default judge is Opus 5.5 (`claude -p`, free under Max). A
  pair containing Opus output goes to Sonnet 5.5, and a pair containing both is
  skipped.
- The rubric (`bench/pairwise.py` `RUBRIC`) covers scope, minimal diff, matching the
  surrounding code, tests, repo invariants and safety. When a task has a
  `source_commit`, the real fix's diff is shown as a reference.
- Verdicts are cached under `<results>/judge/verdicts`, keyed by both diffs, the rubric
  version and the judge, so reruns only pay for new pairs.
- Ratings are a Bradley-Terry fit (ties count half) on an Elo-like scale centred on 0,
  with 90% bootstrap intervals, written to `<results>/judge/ratings.json`.
  `bench report` adds them as a `judge` column and as `judge_rating` / `judge_ci` in the
  JSON.

```bash
python3 -m bench judge                       # all tasks, all pairs
python3 -m bench judge --task chunker-mutation-01 --pairs 20
python3 -m bench report
```


## jomcgi-agent-index (per-role ranking)

`index.yaml` defines what "good" means here, per factory role. Each model gets
a 0..1 score on each axis:

- correctness: floor pass rate;
- frontier: mean graded score on hard and frontier tasks;
- judgement, security and review: tasks tagged with that name under `axes:` in
  task.yaml;
- norms: the mean norms score;
- judge: the pairwise-judge rating from `bench judge`
  (`<results>/judge/ratings.json`, or `--judge-json`), min-max normalised.

Each role (planner, implementer, reviewer) weights the axes. An axis a model
was never measured on drops out and the remaining weights renormalise. The CI
is a bootstrap over the model's tasks, and axes measured on fewer than `min_n`
tasks are flagged. Per role the index names the best model, plus the cheapest
and the fastest model within `tolerance` of it. That pair is the routing
question.

`bench report` appends the index to the markdown and embeds it in the page
JSON under `index`. `bench index [--role implementer] [--json]` prints it on
its own. Re-weighting is a YAML edit; no cell needs re-running.

## Setup

Two interpreters are involved:

- **The harness** runs on your bare `python3` and needs `pyyaml`, `pydantic`, `httpx`
  (plus `pytest` for the unit tests).
- **The verifier venv** runs real monolith code (fixture + gold tests) and must have the
  monolith runtime deps. Recreate it from the pinned list:

  ```bash
  python3 -m venv ~/.cache/model-bench-venv
  ~/.cache/model-bench-venv/bin/pip install -r requirements-venv.txt
  ```

  The `pytest` verifier resolves this venv from `$MODEL_BENCH_VENV` (default
  `~/.cache/model-bench-venv`).

### Providers: candidates vs the Claude ceiling

Each model in `models.yaml` has a `provider`:

- `openrouter` (default, `role: candidate`) rents the model per-token and records real
  cost / turns / tokens. These are the models you would actually deploy, so their cost is
  the point. `OPENROUTER_API_KEY` must be set to run any of them; calls are billed (cents
  per task).
- `claude-code` (`role: anchor`, the Claude models) runs through the local `claude` CLI
  under the Max subscription. It is a **capability ceiling**, not a cost-ranked competitor:
  free, so cost is 0, and it uses Claude Code's own agent harness (not the bench tool
  loop), so its turns/tokens are not comparable to candidates. The judge also runs this
  way. Anchors need the `claude` CLI on PATH and no OpenRouter key.

An anchors-only run (`--model claude`) needs no `OPENROUTER_API_KEY` at all.

### In-cluster llama.cpp (self-hosted qwen)

`bench run --base-url <url>/v1` points the OpenRouter client at any OpenAI-compatible
endpoint instead, skipping the API key and OpenRouter pricing (cost records as 0). Two
routes reach the in-cluster llama.cpp:

- on-cluster: `kubectl -n inference port-forward svc/inference 18080:8080` and
  `--base-url http://127.0.0.1:18080/v1`;
- off-cluster: `--base-url https://private.jomcgi.dev/llm/v1` plus
  `--header "CF-Access-Client-Id: ..." --header "CF-Access-Client-Secret: ..."`.
  Cloudflare Access authenticates on those two headers only and ignores `Authorization`.

The `qwen/qwen3.8-27b` entry in `models.yaml` is the self-hosted row: it is
`status: experimental` so a bare `bench run` never sends that slug to OpenRouter, and its
`api_model` is the alias llama.cpp actually serves. The comments on that entry are
canonical for how it is reached.

## Result cells (billed output — kept out of the worktree)

Each run writes one JSON cell per (task, model) under a **durable per-user cache dir**,
`~/.cache/model-bench/results` by default (override with `MODEL_BENCH_RESULTS` or
`--results`). They are deliberately NOT inside the git worktree: `results/` is gitignored,
so a `git worktree remove` would delete them and force a full paid re-run. The cache is
keyed on prompt + fixture + verifier + model + budget, so re-running skips unchanged
cells; only the committed `reports/leaderboard.md` and the page's `leaderboard.json` are
version-controlled. `bench report --json-out` writes the JSON; the committed copy the
public page reads lives at
`projects/monolith/frontend/src/lib/public/llm-leaderboard/leaderboard.json`.

## Commands

```bash
python3 -m bench snapshot                    # materialize all task fixtures
python3 -m bench run                          # run every active (task, model) cell
python3 -m bench run --task worldcup-swing-settled-01 --model qwen3-coder-30b  # one cell, cheap
python3 -m bench report                       # regenerate reports/leaderboard.md
python3 -m bench list                         # models and their status/role
python3 -m bench drop <id> --reason "..."     # retire a model in models.yaml
python3 -m bench prune                        # delete result cells for retired models
python3 -m bench calibrate --task chunker-mutation-01 --reps 3  # anchor ladder
python3 -m bench calibrate --task <id> --from-json scores.json --write  # record scores
```

The leaderboard uses a **gate model**. Each task carries a difficulty `tier`
(`easy` / `standard` / `hard`): easy + standard form the qualification **floor**. A model
may miss at most one floor task and stay viable (`FLOOR_MISS_TOLERANCE` in `bench/cli.py`,
so a single flaky miss does not exclude it); missing more disqualifies it. The `hard` tasks
differentiate the qualified. Among the qualified, ranking is hard-task pass, then
frontier score, then cost.

`frontier` sits above `hard`. A task is admitted there by `bench calibrate`, which runs
the Claude anchors (Haiku 4.5, Sonnet 5.5, Opus 5.5 via Claude Code, pinned with
`--model`) several times each and checks the means against a ladder: Haiku <= 0.4,
Sonnet in [0.35, 0.8], Opus >= 0.85, strictly increasing (`DEFAULT_LADDER` in
`bench/calibrate.py`; a task may override any bound under `calibration.ladder`). Frontier
tasks are graded on a scale, so the leaderboard shows a model's mean frontier score
(graded `score`, or 1/0 for a binary verifier) rather than a pass count. `--write`
records the result in the task's `calibration:` block. That block is provenance only
and is not part of any cell key, so writing it never invalidates cached cells.
`--from-json` records scores produced elsewhere (e.g. subagent runs graded by hand) in
the form `{"haiku": [0.3, 0.5], "sonnet": [...], "opus": [...]}`.

It reads on two lenses. The **self-host** lens (hard-task pass, median tokens/turns,
tool-use reliability) is model-intrinsic and carries over to local hardware. The **cloud**
lens (median wall-time, cost, cost-per-solve) is the real time and money to rent the model
via OpenRouter, versus the Claude anchor rows. Remote wall-time reflects a typical cloud
request, not local GPU throughput.

## Probing the live qwen lane

`probe/` is a separate CLI that runs the same tasks through the monolith's agent-session
API on the in-cluster qwen and pi lane, recording wall time, diff, verifier result and a
SigNoz span breakdown. It measures the lane, not the model, and is the harness for the
#5051 efficiency loop. See [`probe/README.md`](probe/README.md).
