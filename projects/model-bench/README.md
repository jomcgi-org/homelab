model-bench is an internal Python CLI that screens OpenRouter LLM models against a curated pack of coding and config tasks drawn from this repo's real commits, to identify budget-tier models that clear the quality bar for offloadable work. For the full design rationale, task taxonomy, and scoring approach, see `docs/plans/2026-06-30-model-bench-design.md`.

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

Current agentic tasks: `slo-budget-breach-01` (pure-logic, `command` verifier),
`worldcup-fixtures-guard-01` (httpx parsing, subtle missing-`continue` bug),
`hikes-doability-01` (fastapi/sqlmodel + in-memory SQLite, duration-aware doability).

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

`OPENROUTER_API_KEY` must be set; model calls are billed (cents per task).

## Commands

```bash
python3 -m bench snapshot                    # materialize all task fixtures
python3 -m bench run                          # run every active (task, model) cell
python3 -m bench run --task hikes-doability-01 --model qwen3-coder-30b  # one cell, cheap
python3 -m bench report                       # regenerate reports/leaderboard.md
```

The leaderboard's headline is the agentic table: pass-rate, median tokens, median turns,
cost, and tool-use reliability per model.
