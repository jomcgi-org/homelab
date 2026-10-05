# Evals

Benchmarks over the engine's OpenAI-compatible API, out of process. Every setting is an
argument; task defaults follow the protocol each published score was measured under.

```sh
cd projects/oom-inference/evals
export UV_CACHE_DIR=/disks/nvme-02/src/.toolchains/uv-cache
uv sync
uv run python -m oomeval list                       # tasks, default protocols, published scores
```

## Run

Start `oominf serve` (under `flock /disks/nvme-02/src/oominf-data/gpu.lock`), then:

```sh
uv run python -m oomeval run --label bf16 --tasks gsm8k,ruler --limit 300 \
    --ruler-tokenizer <model>/tokenizer.json --ruler-lengths 8192,32768,95000 \
    --notes "faba5e278 --dense bf16"
```

- Results: `results/<label>/<task>.jsonl` (one record per sample: score, extracted answer,
  tokens, time to first token, decode time, final text) and `<task>.json` (settings).
- A rerun with the same label resumes: finished samples are skipped and failed ones retried.
  Changed settings under an existing label are refused, so two arms never mix.
  `--time-budget <minutes>` stops cleanly, e.g. to fit a background-job limit.
- `--limit N` takes a seeded random subset (`--subset-seed`). Arms run with the same arguments
  see the same items.
- Sampling: `--temperature --top-p --top-k --presence-penalty --max-tokens --samples --seed
  --thinking on|off --reasoning-effort`.

## Compare two arms

```sh
uv run python -m oomeval compare results/bf16 results/fp8
```

Pairs samples by (item, sample) and reports B - A with a paired bootstrap 95% CI, McNemar's
exact test on discordant pairs, the share of identical answers, per-group scores (RULER per
variant and length), completion length, truncation rate and speed. Settings that differ
between the runs are flagged.

For a precision A/B, greedy (`--temperature 0`) on the same subset gives the most power per
sample. A few hundred items resolve differences of about 3-5 points. Small shifts are
measured more sensitively by `oominf score` (KL divergence, top-1 agreement). Run the same
two-arm comparison with only `--prefill-chunk` changed to see the run-to-run noise floor.

## Estimate a pass

```sh
uv run python -m oomeval estimate --run results/bf16 --task gsm8k --arms 2
uv run python -m oomeval estimate --run results/bf16 --task gsm8k --decode-tps 43   # a faster build
uv run python -m oomeval estimate --sgl-metrics <checkpoint>/aime26_metrics.json --prefill-tps 100 --decode-tps 32
```

One stream: time per sample is time to first token plus completion tokens over decode tok/s.
Reasoning tokens dominate thinking-mode tasks.

## Tasks

| task | items | notes |
|---|---|---|
| `gsm8k` | 1319 | boxed numeric answer; card: 97.27 for this checkpoint (SGLang) |
| `aime26`, `aime25` | 30 | MathArena; card: 98.75 pass@1 over 8 samples, thinking, max 130k |
| `gpqa_diamond` | 198 | ungated mirror with shuffled choices; Qwen card: 91.7 (bf16 base) |
| `mmlu_pro` | 12,032 | `--mmlu-pro-categories` to subset |
| `ruler` | generated | single, multikey, multivalue needles and variable tracking at `--ruler-lengths`, in Paul Graham essays (`--ruler-haystack essay`, default) or RULER's repeated noise sentences (`noise`, easy) |

Published scores come from other engines and harnesses (prompts, system prompt, answer
extraction, sample counts), so they are a sanity check, not an A/B arm. Compare arms run
here.
