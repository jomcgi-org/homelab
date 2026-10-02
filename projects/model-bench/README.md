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
respectively). The pack currently has 19 agentic and 3 single-shot tasks in total; `tasks/`
is the source of truth for the full, current list.

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
