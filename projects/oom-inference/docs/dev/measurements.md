# Measurements

Dated measurement logs: what was run, under which conditions, and what it
showed. The decisions they support are the **Why** paragraphs in
[architecture.md](architecture.md); the user-facing summary of the hardware runs
is the [hardware guide](../guide/hardware.md). How to measure so the numbers
mean something is in [the contributor overview](README.md#measuring-performance).

Shorter measurements stay in the **Why** paragraph they support: KV cache
format (2026-10-04, #6830), stage-ahead and staging copies, the prefix store
(#6859) and prefill slicing.

oom-inference sizes itself from fresh checks on every start (usable RAM including
a container's limit, free VRAM, which read path works) and shrinks instead of
failing; the design and its rules are in
[architecture.md](architecture.md#smaller-machines). The sections below record
what was measured when smaller machines were simulated on the reference box, and
what was not.

`oominf doctor --model model.oom` shows what `serve` would decide on a machine
without starting it.

## Reference box

RTX 4090 (24 GB), Ryzen 7 7800X3D (8 cores, 16 threads), 62 GiB RAM, Kingston
KC3000 2 TB NVMe (ext4). Probe: 6.9 GB/s consecutive and 7.1 GB/s scattered record
reads, 0.7 ms per record alone, 26.8 GB/s pinned host to device, 57 us per host
expert on 8 threads.

## Simulated machines (October 6, 2026)

Every run: `oominf serve --dense fp8 --expert-precision bf16
--attention-precision bf16`, otherwise defaults (2 streams, 32k context), in a
transient systemd scope (`systemd-run --user --scope -p MemorySwapMax=0
[-p MemoryMax=...]`). Requests, in order: a short prompt ("What is the capital of
France? Answer with one word."), the 21.9k-token research demo prompt twice with
fresh prefixes (first with cold expert tiers, then warm), the short prompt again,
temperature 0, thinking off. "Correct" means both short answers are "Paris" and
both long answers are valid newline-delimited JSON (every line parses). Prefill is
prompt tokens over time to first token; decode is completion tokens after the
first over the time after it. Peak is the scope's `memory.peak` (pinned tiers,
heap and page cache charged to it).

| Config                                                       | Outcome                                                                                         | Usable host | Tiers: VRAM / host (GiB) | Prefill tok/s cold / warm | Decode tok/s cold / warm | Correct                      | Peak (GiB) |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- | ----------- | ------------------------ | ------------------------- | ------------------------ | ---------------------------- | ---------- |
| No limit (62 GiB)                                            | serves                                                                                          | 56.1        | 16.4 / 44.3              | 2,402 / 3,118             | 37.5 / 45.2              | yes                          | 50.9       |
| `MemoryMax=48G`                                              | serves, smaller host tier                                                                       | 46.5        | 16.4 / 34.7              | 2,599 / 3,076             | 28.4 / 33.0              | yes                          | 37.9       |
| `MemoryMax=32G`                                              | serves, smaller host tier                                                                       | 30.5        | 16.4 / 18.7              | 2,595 / 2,749             | 15.5 / 15.7              | yes                          | 20.6       |
| `MemoryMax=24G`                                              | serves                                                                                          | 22.5        | 16.4 / 10.7              | 2,374 / 2,450             | 11.4 / 12.0              | yes                          | 12.6       |
| `MemoryMax=20G`                                              | serves                                                                                          | 18.5        | 16.4 / 6.7               | 1,848 / 2,317             | 9.8 / 10.0               | yes                          | 8.6        |
| `MemoryMax=16G`                                              | serves                                                                                          | 14.5        | 16.4 / 2.7               | 2,105 / 2,193             | 8.6 / 9.0                | yes                          | 4.6        |
| `MemoryMax=16G`, `--host-reserve-gib 4`                      | serves                                                                                          | 14.5        | 16.4 / 8.6               | 2,305 / 2,377             | 11.1 / 10.6              | yes                          | 10.6       |
| `MemoryMax=12G`                                              | serves at the minimum; reserve lowered 10 to 7.9 GiB                                            | 10.5        | 16.4 / 0.8               | 824 / 847                 | 12.7 / 12.7              | yes                          | 3.8        |
| `MemoryMax=10G`                                              | serves at the minimum; reserve lowered to 5.9 GiB                                               | 8.5         | 16.4 / 0.8               | 781 / 785                 | 12.8 / 12.6              | yes                          | 3.9        |
| `MemoryMax=8G`                                               | serves at the minimum; reserve lowered to 3.9 GiB                                               | 6.5         | 16.4 / 0.8               | 818 / 845                 | 12.9 / 12.7              | yes                          | 3.8        |
| `MemoryMax=7G`                                               | refuses to start (below)                                                                        | 5.5         | n/a                      | n/a                       | n/a                      | n/a                          | n/a        |
| `MemoryMax=6G`                                               | refuses to start                                                                                | 4.5         | n/a                      | n/a                       | n/a                      | n/a                          | n/a        |
| `MemoryMax=32G`, `--prefix-store-dir`, `--kv-placement host` | serves; beside the tiers: host KV 1.1, checkpoints 1.3, snapshots 3.4 GiB                       | 30.5        | 16.4 / 14.2              | 2,444 / 2,381             | 13.3 / 13.4              | yes (4 of 5 runs, see below) | 18.9       |
| `taskset -c 0-3` (4 CPUs)                                    | serves; probe: 204 us per expert on 2 threads, host compute stays on                            | 56.6        | 16.4 / 44.7              | 2,320 / 3,114             | 31.8 / 35.9              | yes                          | 47.6       |
| `taskset -c 0,1` (2 CPUs)                                    | serves; probe: 429 us per expert on 1 thread, more than 2x the 103 us copy, so host compute off | 56.7        | 16.4 / 44.9              | 2,319 / 3,033             | 32.1 / 34.7              | yes                          | 50.9       |
| `--io pread` (io_uring unavailable)                          | serves; logs "O_DIRECT pread on a thread pool"                                                  | 56.2        | 16.4 / 44.3              | 2,369 / 3,115             | 36.6 / 45.6              | yes                          | 50.7       |
| `--io buffered` (no O_DIRECT)                                | serves; logs "buffered pread on a thread pool"                                                  | 56.6        | 16.4 / 44.7              | 1,630 / 2,505             | 23.4 / 30.2              | yes                          | 52.0       |

The 7 GiB refusal, verbatim:

    oominf: model load failed: host memory: the smallest working expert tiers need
    0.8 GiB + 0.5 GiB for transfer and work buffers + 1.3 GiB for prefix
    checkpoints + 3.0 GiB reserve = 5.6 GiB, but only 5.5 GiB is usable; to fit,
    raise the memory (or container) limit, lower --max-context or --max-streams
    (fewer prefix checkpoints), drop --prefix-store-dir or --kv-placement host, or
    lower --host-reserve-gib

**Tested minimum RAM: an 8 GiB container (no swap) with the default 32k context
and 2 streams** serves the 22k-token prompt correctly, at about a third of the
reference prefill rate. The minimum is set by the planner, not by what the process
used (3.8 GiB peak): 0.8 GiB of smallest tiers, 1.8 GiB beside them and a 3 GiB
floor for the reserve. Lower limits are refused at start-up with the message above
rather than killed later. Fewer streams or a shorter context lower it further; not
measured.

Observations:

- Decode speed follows the host tier: every GiB the tiers lose is decode misses
  read from disk (about 35 GiB of the host tier at 48G, 19 at 32G).
- At the minimum (12 GiB and below) prefill drops to about 800 tok/s: the staging
  ring is one record, so prefill reads that find no free host slot go through it
  one at a time. Decode there is faster than at 16 to 24 GiB, which led to the
  next point.
- **Lookahead hurts in these runs.** With `--lookahead off`, warm decode of the
  demo was 12.8 instead of 8.4 to 9.0 tok/s at 16G, 20.2 instead of 15.7 at 32G,
  39.3 instead of 33.0 at 48G, and 49.7 instead of 45.2 without a limit. At the
  minimum, lookahead is already capped to nothing (it may not pin the slots a
  fetch needs). `oominf bench` (one sequence, no server) does not show it: 56.8
  against 58.6 tok/s short, 46.0 against about 45.5 at 32k. Three warm requests
  per setting confirmed it (45.4 against 48.4 tok/s with no limit, 16.5 against
  21.2 at 32G), so `--lookahead` now defaults to off; the rows above were measured
  with it on. See [Decode lookahead](#decode-lookahead).
- At 16G the default 10 GiB reserve is mostly unused (peak 4.6 GiB); 4 GiB raised
  the host tier from 2.7 to 8.6 GiB and decode from 9.0 to 10.6 tok/s warm.
- The 2-CPU run's slower start (58 s to ready against 26 to 29 s) is the FP8
  quantization of the dense weights on two cores.
- Buffered reads cost about 20% of prefill and a third of decode against O_DIRECT
  through io_uring; O_DIRECT `pread` matches io_uring here.

One unexplained failure: the first `--prefix-store-dir --kv-placement host` run at
32G ended its first long request after 9.3 s with no output; the server log said
nothing, because failed requests were not logged (they are now). Four further runs
of the same configuration, two with the logging, all served correctly.

## Speed modes

Against the exact reference, max-perf flags one at a time, 32k and 95k-token
prompts. Supports the speed modes being off by default
([Precision](architecture.md#precision)).

- `--dense fp8`: half the bytes, so decode reads less (35-37 ms per verify step
  against 43-50 ms at 32k-95k) and the expert tier gains about 1,660 VRAM slots.
  Next-token distributions shift measurably (KL about 0.10-0.13, 86-88% top-1
  against the exact reference, about 3x the rounding floor, with per-row or
  per-block scales and with or without bf16 routers), while retrieval and the
  long-context task set still pass (#6865).
- `--expert-precision bf16`: the kernel runs 2.2x faster and warm 32k prefill
  drops from 16.2 s to 14.0 s. KL 0.058 / 92.1% top-1 at 32k and 0.035 / 91.2%
  at 95k, about at the rounding floor, and `check-model` stays inside the HF
  bf16 budget (#6838). Decode is unaffected.
- `--attention-precision bf16`: KL 0.051 / 92.6% top-1 at 32k and 0.031 / 92.4%
  at 95k, inside the rounding floor. The kernel is bound by gathering each
  token's selected fp32 K/V rows, so it is only about 15% faster (1.51 to 1.27 s
  per 32k prefill).
- `--kv-placement host`: results are identical to device placement. It frees
  the cache's VRAM for experts: decode is 4% slower at 32k and 9% faster at
  95k, prefill 4-6% slower (each layer's fp32 shadow is read over PCIe) (#6856).
  With `--dense fp8` the expert tier already has the VRAM and host placement is
  slower at every length measured (32k-95k).

## Task accuracy, exact against max-perf (October 7, 2026)

Paired task runs with `oomeval`, exact (`serve` defaults) against max-perf
(`--dense fp8 --expert-precision bf16`), the same items in both arms,
temperature 0. GSM8K, MMLU-Pro and RULER at 8k and 32k ran on a0456e56c; RULER at
95k ran on the build with the fragmentation fix below.

| Task (items)                      | Exact | Max-perf | Max-perf - exact (95% CI) | Decode tok/s (exact, max-perf) |
| --------------------------------- | ----- | -------- | ------------------------- | ------------------------------ |
| GSM8K (300 of 1,319)              | 97.33 | 97.00    | -0.33 (-2.00 to +1.00)    | 33.0, 39.6                     |
| MMLU-Pro (200 of 12,032)          | 77.00 | 77.50    | +0.50 (-2.50 to +3.50)    | 35.5, 42.4                     |
| RULER 8k and 32k (104)            | 99.23 | 99.62    | +0.38                     | 18.6, 20.4                     |
| RULER 95k (52)                    | 98.46 | 98.08    | -0.38 (-1.15 to 0.00)     | 16.7, 18.0                     |

- No task moves beyond its interval: McNemar p = 1.0 on GSM8K (3 items right only
  in exact, 2 only in max-perf; 98% the same answer) and MMLU-Pro (5 and 6; 93%).
  Every RULER difference is variable tracking (one more or one fewer variable
  found); single needle, multi-key and multi-value score 100 in both arms at
  every length.
- MMLU-Pro ran with `--max-tokens 4096` (the task default is 8192): 17.5% of
  answers in both arms were cut off, so its absolute score understates the
  model; the paired difference is unaffected.
- Max-perf answers are slightly longer (GSM8K 405 against 390 completion tokens,
  MMLU-Pro 1,407 against 1,359). 95k TTFT: 29.7 s max-perf, 36.9 s exact.

Max-perf first failed 5 of 8 95k RULER requests with `CUDA_ERROR_OUT_OF_MEMORY`
(#6854). Live device memory was within the prefill's estimate, but the
stream-ordered pool's reserved memory grew about 65 MB per layer beyond it (3 GB
by the last layer of a 95k prefill): small long-lived buffers allocated between
the prefill's large transient ones (prefix-checkpoint copies, and FP8 weights
dequantized for cuBLAS) each pinned a pool block. Allocating the checkpoint
copies before the prefill and keeping the dequantized weights in one arena left
2.3 GB free at the last layer instead of 0.6 GB, and all 104 95k requests above
completed.

## Published-protocol runs (October 8, 2026)

The model-card evals: the files published as
`jomcgi-org/Qwen3.8-Flash-Next-NVFP4-oominf` (`oominf-format-0`), `serve`
defaults, build 069b1eec3, one request at a time, each task's default
`oomeval` protocol (the published one).

| Task (items)              | Protocol                                     | oominf           | Published                                        |
| ------------------------- | -------------------------------------------- | ---------------- | ------------------------------------------------ |
| GSM8K (1,319)             | t0.6, top-p 0.95, max 8,192, 1 sample        | 97.73 (CI ±0.80) | RadixArk: BF16 97.12 to 97.50, SGLang NVFP4 97.27 |
| GPQA-Diamond (198)        | thinking, t1.0, top-p 0.95, top-k 20, max 65,536 | 88.1 on the first 118 (CI ±5.8), partial | Qwen BF16 91.7 (harness not stated)              |
| AIME26 (30 x 8)           | thinking, t1.0, top-p 0.95, max 130,000      | not yet run      | RadixArk: BF16 100, SGLang NVFP4 98.75           |

- GSM8K: 0.3% of answers hit the token limit; 471 completion tokens and 37.2
  tok/s median decode; 4.9 h wall.
- GPQA-Diamond was paused to free the GPU after 118 questions, the first 118
  rows of the dataset file (not a random subset; the file has no subject field,
  so a subject bias cannot be ruled out). Answers that hit the 65,536-token limit
  (4 of 118) score as wrong. Answers run 8k tokens at the median, about 7
  minutes each.
  `oomeval` resumes it by label (`hf-gpqa`).

## Decode lookahead

October 6-7, 2026, the served demo, warm, three requests per setting. Supports
lookahead being off by default (commit `2ce6fe7b4`,
[A decode step](architecture.md#a-decode-step)).

- Prediction: 62-65% precision over all predicted experts; about 24% of
  lookahead reads were then used.
- No memory limit: 45.4 tok/s with lookahead against 48.4 without. Demand disk
  reads per token were 20.6 against 20.8: the used reads saved none, since a
  placed guess counts as a fresh access and evicts records that would have hit,
  and lookahead added 8.7 reads per token on top.
- 32 GiB limit: 16.5 against 21.2 tok/s. The drive is the bottleneck there
  (74-78% of decode time waits on reads) and lookahead raised reads per token
  from 88 to 136, close to the drive's 7 GB/s.
- Prediction kept but no reads issued: 48.7 tok/s, matching off, so the cost is
  the reads, not the bookkeeping (0.1-0.9% of decode time).
- `oominf bench` hides it: its workload reads about 4 records per token from
  disk.

The [simulated-machine runs](#simulated-machines-october-6-2026) showed it first.

## Prompt lookup

`bench/lookup.py`, medians of three warm runs, max-perf config (#6872): +22 to
+40% output rate on file edits (92 to 95% of lookup drafts kept, about 7 tokens
per step), level on tests, -2 to -9% on prose, quotes and a short diff. An
8-token verification step costs 110 to 125 ms when its tokens are new (about
2,200 routed records, a quarter outside VRAM) against about 25 ms for one token.
Before the decode-kernel paths, an 8-token step fell onto the sparse prefill
attention and dequantized FP8 weights for cuBLAS every step (215 ms).

Since the docs split, the quote workload reads the moved
[architecture.md](architecture.md), so its results are not comparable with runs
before it.

## Draft-head catch-up

The demo, sampled, serve defaults, warm. Supports keeping 256 residuals across
lookup steps ([Speculative decoding](architecture.md#speculative-decoding)).

- Before: model drafts kept 76% with lookup on against 83% with it off; decode
  43.4 against 47.0 tok/s (medians of 12 and 4 requests).
- Catching up: 83% acceptance and 45.6 tok/s with lookup on (median of 10
  requests); lookup off unchanged within noise (46.1 tok/s).
- The `bench/lookup.py` file-edit workloads: within -4 to +2% of before on
  every workload (medians of five warm passes, which vary by a few percent
  between server runs). Rings of 32, 64 and 256 rows measured the same.
- `tests/mtp_lookup.rs`: 0 of 48 draft tokens differ from drafts made on every
  step, against 13 when the head restarts.
- Rejected: a draft width growing from one to three tokens while recent model
  drafts were mostly kept (90% for two, 95% for three) tied one-token drafts on
  the demo (46.4 against 46.3 tok/s) and lost 3 to 7% on the edit workloads,
  whose high acceptance comes from easy positions lookup leaves the head. Fixed
  two- and three-token drafts ran the demo at 45.6 and 42.8 tok/s.

## Concurrent requests

Supports one batched step for all decoding streams
([Concurrent requests](architecture.md#concurrent-requests)).

`oominf bench --streams` (max-perf config, warm tiers, 16 different requests,
one token per sequence per step): 1 sequence 22 ms per step (45 tok/s), 2
sequences 37 ms (54 tok/s), 4 sequences 52-55 ms (73-77 tok/s), 8 sequences
115-120 ms (67-70 tok/s), 16 sequences 254-257 ms (62-63 tok/s). The plateau
comes early because unrelated requests share few experts: 8 sequences route
about as many records per step as 8 verified drafts of one sequence (2,266
against 2,317), but across steps their working set outgrows the VRAM tier (hit
rate 68% against 84%), so host-tier copies dominate. Running each sequence's
token mixer on its own first cost about 10 ms per extra sequence (its dense
GEMVs and about 25 kernel launches per layer); batching every row-wise
operation removed that (8 sequences 146 to 120 ms per step).

Through the server (`bench/http_bench.py --concurrency`, K concurrent streaming
requests of 128 tokens with different short prompts, temperature 0, the same
max-perf config; median of two rounds):

| K   | `--max-streams 1`: aggregate tok/s | TTFT p50 / max (s) | `--max-streams 8`: aggregate tok/s | per-stream tok/s | TTFT p50 / max (s) | ITL p50 / p95 / max (ms) |
| --- | ---------------------------------- | ------------------ | ---------------------------------- | ---------------- | ------------------ | ------------------------ |
| 1   | 43.4                               | 0.55 / 0.55        | 43.6                               | 53.4             | 0.55 / 0.55        | 25 / 41 / 58             |
| 2   | 42.6                               | 0.64 / 3.6         | 48.0                               | 31.3             | 0.64 / 1.3         | 40 / 56 / 675            |
| 4   | 41.5                               | 6.7 / 9.9          | 54.9                               | 16.9             | 2.1 / 2.8          | 51 / 68 / 739            |
| 8   | 39.9                               | 13.4 / 23.1        | 47.3                               | 7.1              | 3.7 / 5.9          | 126 / 210 / 812          |

With `--max-streams 4` the same runs gave 47.3, 47.8 and 47.4 tok/s at K = 2,
4 and 8 (K = 8 queues four requests: TTFT p50 9.5 s); the two K = 4 runs,
which schedule identically, differ by 15% (54.9 against 47.8), the run to run
spread of these numbers. One request decodes exactly as before (MTP drafts, 53
tok/s per stream). With several, aggregate throughput rises 1.1-1.3x and the
queueing delay before a request's first token falls 3-4x, at the cost of each
stream's speed. The aggregate gain is smaller than the steady state above
because each new request's prefill (about 0.6 s for these prompts, a sweep of
most layers' experts) stalls the others' decoding (the 0.7-0.8 s maximum gaps),
and one stream alone already gains about 1.2x from its drafts, which batched
steps mostly drop (the budget finds them not worth the width beside other
streams' next tokens).

## Overload

`--max-queued 4` (2 streams), 24 clients sending requests back to back for 90 s,
alternating the OpenAI and Anthropic APIs, each retrying 0.2 s after a 429
(ignoring `Retry-After` on purpose, to hammer): 100 requests served (52 OpenAI, 48
Anthropic), 8,064 refused with 429 and `Retry-After: 5` (4,033 and 4,031), none
failed. The scope's memory stayed between 48.47 and 48.65 GiB (pinned tiers
included) over 95 s.

## Not verified

- **Slower disk.** cgroup v2 `io.max` needs the `io` controller, which this
  machine's user manager does not delegate (`cpu memory pids`), so
  `IOReadBandwidthMax` had no effect without root. Not measured on real hardware.
  Slow and failing reads are covered only by the tier fault-injection tests
  ([testing.md](testing.md#tier-fault-injection)), and the profile's slow-drive warnings are untested on a slow
  drive.
- **io_uring or O_DIRECT actually unavailable** (seccomp, an old kernel, a
  filesystem without O_DIRECT). Only the forced fallbacks (`--io pread`,
  `--io buffered`) were run here; automatic selection was exercised in unit tests
  on this machine, where every path works.
- **Smaller GPUs.** Every run had the 4090's VRAM; the VRAM side of the planner
  (shrinking, lowering the reserve, dropping the stage, refusing below the minimum)
  is covered by unit tests and the mock device only.
- **cgroup v1** limits: parsing is unit-tested; no v1 machine was available.
- Other GPU models, drivers, and non-x86 hosts.

## Reproducing

    # RAM cap (a user scope; needs the memory controller delegated, as here)
    systemd-run --user --scope -p MemoryMax=16G -p MemorySwapMax=0 \
        oominf serve --model model.oom
    # Fewer CPUs
    taskset -c 0-3 oominf serve --model model.oom
    # Read-path fallbacks
    oominf serve --model model.oom --io pread
    oominf serve --model model.oom --io buffered
    # Overload: a small queue, then many concurrent clients
    oominf serve --model model.oom --max-queued 4
