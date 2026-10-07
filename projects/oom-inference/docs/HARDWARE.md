# Running on other hardware

oom-inference sizes itself from fresh checks on every start (usable RAM including
a container's limit, free VRAM, which read path works) and shrinks instead of
failing; the design and its rules are in
[ARCHITECTURE.md](ARCHITECTURE.md#smaller-machines). This page records what was
measured when smaller machines were simulated on the reference box, and what was
not.

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

| Config | Outcome | Usable host | Tiers: VRAM / host (GiB) | Prefill tok/s cold / warm | Decode tok/s cold / warm | Correct | Peak (GiB) |
|---|---|---|---|---|---|---|---|
| No limit (62 GiB) | serves | 56.1 | 16.4 / 44.3 | 2,402 / 3,118 | 37.5 / 45.2 | yes | 50.9 |
| `MemoryMax=48G` | serves, smaller host tier | 46.5 | 16.4 / 34.7 | 2,599 / 3,076 | 28.4 / 33.0 | yes | 37.9 |
| `MemoryMax=32G` | serves, smaller host tier | 30.5 | 16.4 / 18.7 | 2,595 / 2,749 | 15.5 / 15.7 | yes | 20.6 |
| `MemoryMax=24G` | serves | 22.5 | 16.4 / 10.7 | 2,374 / 2,450 | 11.4 / 12.0 | yes | 12.6 |
| `MemoryMax=20G` | serves | 18.5 | 16.4 / 6.7 | 1,848 / 2,317 | 9.8 / 10.0 | yes | 8.6 |
| `MemoryMax=16G` | serves | 14.5 | 16.4 / 2.7 | 2,105 / 2,193 | 8.6 / 9.0 | yes | 4.6 |
| `MemoryMax=16G`, `--host-reserve-gib 4` | serves | 14.5 | 16.4 / 8.6 | 2,305 / 2,377 | 11.1 / 10.6 | yes | 10.6 |
| `MemoryMax=12G` | serves at the minimum; reserve lowered 10 to 7.9 GiB | 10.5 | 16.4 / 0.8 | 824 / 847 | 12.7 / 12.7 | yes | 3.8 |
| `MemoryMax=10G` | serves at the minimum; reserve lowered to 5.9 GiB | 8.5 | 16.4 / 0.8 | 781 / 785 | 12.8 / 12.6 | yes | 3.9 |
| `MemoryMax=8G` | serves at the minimum; reserve lowered to 3.9 GiB | 6.5 | 16.4 / 0.8 | 818 / 845 | 12.9 / 12.7 | yes | 3.8 |
| `MemoryMax=7G` | refuses to start (below) | 5.5 | n/a | n/a | n/a | n/a | n/a |
| `MemoryMax=6G` | refuses to start | 4.5 | n/a | n/a | n/a | n/a | n/a |
| `MemoryMax=32G`, `--prefix-store-dir`, `--kv-placement host` | serves; beside the tiers: host KV 1.1, checkpoints 1.3, snapshots 3.4 GiB | 30.5 | 16.4 / 14.2 | 2,444 / 2,381 | 13.3 / 13.4 | yes (4 of 5 runs, see below) | 18.9 |
| `taskset -c 0-3` (4 CPUs) | serves; probe: 204 us per expert on 2 threads, host compute stays on | 56.6 | 16.4 / 44.7 | 2,320 / 3,114 | 31.8 / 35.9 | yes | 47.6 |
| `taskset -c 0,1` (2 CPUs) | serves; probe: 429 us per expert on 1 thread, more than 2x the 103 us copy, so host compute off | 56.7 | 16.4 / 44.9 | 2,319 / 3,033 | 32.1 / 34.7 | yes | 50.9 |
| `--io pread` (io_uring unavailable) | serves; logs "O_DIRECT pread on a thread pool" | 56.2 | 16.4 / 44.3 | 2,369 / 3,115 | 36.6 / 45.6 | yes | 50.7 |
| `--io buffered` (no O_DIRECT) | serves; logs "buffered pread on a thread pool" | 56.6 | 16.4 / 44.7 | 1,630 / 2,505 | 23.4 / 30.2 | yes | 52.0 |

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
  with it on. See ARCHITECTURE.md, "Why no decode lookahead".
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
  (`docs/TESTING.md`), and the profile's slow-drive warnings are untested on a slow
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

## Apple Silicon

Initial direct Metal measurements on a 16 GiB M1 Pro (10 CPU cores), with other
applications running. Model: AxionML/Qwen3.5-35B-A3B-NVFP4, 23,590,338,856-byte
source safetensors, SHA256
`095c234e5e455349e4b6ce69ca61869a61b4527cd392224b517e60da1da1da4f`.
The converted text decoder is 19.59 GiB: 1543 dense tensors and 10,240 expert
records. Every converted checksum passed.

    /usr/bin/time -l target/release/oominf bench --model model.oom \
      --prompt 'Write a short poem about the sea.' --tokens 32 --draft 0 \
      --vram-expert-gib 2

| Measurement | Result |
|---|---|
| Model load | 2.3 s |
| Cold prefill, 18 tokens | 6.44 s |
| Cold decode, 32 tokens | 372.0 ms/token, 2.69 tokens/sec |
| Warm prefill, 18 tokens | 7.55 s |
| Warm decode, 32 tokens | 398.2 ms/token, 2.51 tokens/sec |
| Shared expert cache after shrinking | 1.4 GiB |
| Decode expert cache hits | About 40% |
| Peak process memory footprint (`time -l`) | 3,992,282,496 bytes (3.72 GiB) |
| Maximum resident set size | 2,617,769,984 bytes (2.44 GiB) |
| Swaps reported for this process | 0 |

These are short-context baseline results. The warm run retained an expert cache
far smaller than all experts and did not improve throughput. The SSD-only probe
(3.72 GB/s with eight readers) does not measure inference. System-wide swap
changes, sustained thermal behavior and long-context throughput remain unmeasured.

The local server returned `The capital of France is Paris.` with seven completion
tokens and `finish_reason: stop` for this request:

    target/release/oominf serve --model model.oom --max-context 256 \
      --vram-expert-gib 2 --served-model-name qwen35-mac
    curl localhost:8091/v1/chat/completions -H 'Content-Type: application/json' \
      --data '{"model":"qwen35-mac","messages":[{"role":"user","content":"What is the capital of France? Answer in one sentence."}],"temperature":0,"max_tokens":16,"chat_template_kwargs":{"enable_thinking":false}}'
