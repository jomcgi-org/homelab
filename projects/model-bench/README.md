# model-bench

model-bench is an internal Python CLI that screens OpenRouter LLM models against a curated pack of coding and config tasks drawn from this repo's real commits, to identify budget-tier models that clear the quality bar for offloadable work.

## Two run modes

- **single-shot** (`mode: single-shot`, the default): the model emits a whole file or
  answer in one turn; a deterministic verifier or an LLM judge grades it.
- **agentic** (`mode: agentic`): the model is dropped into a real repo snapshot with
  file tools (`list_dir`/`read_file`/`write_file`/`done`) and edits the code itself over
  several turns. This is the primary contract: native tool-calling carries file content
  in API-serialized JSON, so the output-format noise that dominated single-shot is gone,
  and it measures agentic reliability + token/turn efficiency, not just capability.

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
respectively). The pack currently has 9 agentic and 4 single-shot tasks in total; `tasks/`
is the source of truth for the full, current list.

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

## Result cells (billed output — kept out of the worktree)

Each run writes one JSON cell per (task, model) under a **durable per-user cache dir**,
`~/.cache/model-bench/results` by default (override with `MODEL_BENCH_RESULTS` or
`--results`). They are deliberately NOT inside the git worktree: `results/` is gitignored,
so a `git worktree remove` would delete them and force a full paid re-run. The cache is
keyed on prompt + fixture + verifier + model + budget, so re-running skips unchanged
cells; only the committed `reports/leaderboard.md` and the page's `leaderboard.json` are
version-controlled.

## Commands

```bash
python3 -m bench snapshot                    # materialize all task fixtures
python3 -m bench run                          # run every active (task, model) cell
python3 -m bench run --task worldcup-swing-settled-01 --model qwen3-coder-30b  # one cell, cheap
python3 -m bench report                       # regenerate reports/leaderboard.md
```

The leaderboard uses a **gate model**. Each task carries a difficulty `tier`
(`easy` / `standard` / `hard`): easy + standard form the qualification **floor**, so a
model must pass all of them to be viable (else it is disqualified), and the `hard` tasks
differentiate the qualified. Among the qualified, ranking is hard-task pass, then cost.

It reads on two lenses. The **self-host** lens (hard-task pass, median tokens/turns,
tool-use reliability) is model-intrinsic and carries over to local hardware. The **cloud**
lens (median wall-time, cost, cost-per-solve) is the real time and money to rent the model
via OpenRouter, versus the Claude anchor rows. Remote wall-time reflects a typical cloud
request, not local GPU throughput.
