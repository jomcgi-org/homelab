# Decisions

Each record says why the engine works the way [architecture.md](architecture.md)
describes, what was measured, what it costs, and the concrete signal that should
reopen it. Measurements keep the dates, configurations and issue numbers they
were taken under; "max-perf config" means `--dense fp8 --expert-precision bf16
--attention-precision bf16` on the reference box (RTX 4090, Ryzen 7 7800X3D,
62 GiB RAM, Kingston KC3000 NVMe), and "the demo" is the 22k-token incident
report prompt with about 580 tokens of newline-delimited JSON out.

When a decision changes, edit its record (and the architecture text) in the same
change, keeping the old evidence if it still explains the history.

| Record                                                   | Decision                                                     | Revisit when                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| [D1](#d1-crate-boundaries)                               | Crates split by platform and by model family                 | A second backend or model family needs to change the other axis              |
| [D2](#d2-weights-exact-lossy-modes-opt-in)               | Weights exact; lossy modes opt-in                            | A lossy mode matches the exact path on outcomes and is worth defaulting      |
| [D3](#d3-k8v6-kv-cache-by-default)                       | `k8v6` KV cache by default                                   | Outcome scoring leaves the fp32 band, or VRAM stops limiting the expert tier |
| [D4](#d4-speed-modes-off-by-default)                     | `--dense fp8`, bf16 prefill modes and host KV off by default | Evals confirm a mode at the rounding floor, or long contexts dominate        |
| [D5](#d5-an-explicit-pinned-host-tier)                   | Pinned host tier, not the page cache                         | Page cache or a pager wins on recorded traces                                |
| [D6](#d6-host-compute)                                   | Host-tier hits computed on the CPU                           | Copies get cheap relative to CPU compute, or novel prompts dominate          |
| [D7](#d7-stage-ahead-in-prefill)                         | Stage the next layer's predicted experts                     | Prediction coverage falls well below 90%                                     |
| [D8](#d8-staging-copies-trickle)                         | Stage-ahead copies in small pieces                           | The copy engine can reorder by priority                                      |
| [D9](#d9-no-decode-lookahead-by-default)                 | Decode lookahead off                                         | A workload uses more than about half of lookahead reads                      |
| [D10](#d10-size-from-fresh-checks-shrink-before-failing) | Size from fresh checks; shrink before failing                | A real smaller machine fails where the simulation served                     |
| [D11](#d11-the-profile-decides-only-two-things)          | Profile sets only host compute and the staging ring          | Data from other machines shows another rate-driven default                   |
| [D12](#d12-prefix-store-and-checkpoints)                 | Prefix store and checkpoints                                 | Restoring approaches prefill time                                            |
| [D13](#d13-mtp-speculative-decoding)                     | MTP drafts verified in one step                              | Acceptance falls below the measured 55%                                      |
| [D14](#d14-prompt-lookup-on-by-default)                  | Prompt lookup on by default                                  | Prose-heavy workloads dominate                                               |
| [D15](#d15-keep-residuals-across-lookup-steps)           | Keep 256 residuals for the draft head                        | Catch-up cost grows, or a variable draft width wins on both workload kinds   |
| [D16](#d16-one-batched-step)                             | Concurrent requests share one batched step                   | Many streams stop thrashing the VRAM tier                                    |
| [D17](#d17-prefills-not-sliced-by-default)               | Whole prefills by default                                    | Decoding rows can ride along inside a prefill                                |
| [D18](#d18-fixed-references-and-checked-protocols)       | Fixed references and TLA+ specs                              | A new concurrent protocol lands without a spec                               |

## D1: Crate boundaries

**Context.** New hardware and new models are the two expected kinds of growth.

**Decision.** Only `oominf-cuda` knows about CUDA; only `oominf-models-*`, the
converter's adapter and the server's output parser know about a model family;
the CLI is the one place that names the CUDA backend
([Crates](architecture.md#crates)).

**Evidence.** A platform port implements `Backend` and reuses every model, the
tiers and the server; a new model family reuses the backend, the tiers and the
server. Neither touches the other.

**Tradeoffs.** Operations are described by what models need, so a new
operation goes into an `oominf-core` trait and must be implemented in every
backend. Monomorphised `B: Backend` code compiles once per backend.

**Revisit when.** A second backend or a second model family lands and cannot be
written without changing code on the other axis (a model needing a
backend-specific type, or a backend needing a model-specific kernel interface).

## D2: Weights exact, lossy modes opt-in

**Context.** The engine exists to run a frontier model on modest hardware.

**Decision.** Weights are used exactly as released; runtime precision is reduced
only where the hardware gains from it, and any such mode is opt-in and must pass
the gates in [testing.md](testing.md) ([Precision](architecture.md#precision)).

**Evidence.** The engine exists to run a frontier model on modest hardware
without making it worse. Rounding that buys nothing is pure loss, and silent
precision trades are how engines drift from the model they claim to run.

**Tradeoffs.** The exact defaults leave speed on the table ([D4](#d4-speed-modes-off-by-default)).
The KV cache is the one lossy default ([D3](#d3-k8v6-kv-cache-by-default)).

**Revisit when.** A lossy mode matches the exact path on outcomes (`oominf
score` inside the fp32 rounding band, retrieval and the task evals in
[`evals/`](../../evals/README.md) unchanged) and its speed gain is worth making
it the default; then record that mode as its own decision.

## D3: k8v6 KV cache by default

**Context.** KV memory competes with the VRAM expert tier, and decode speed
follows the VRAM hit rate.

**Decision.** The KV cache is compressed by default, `k8v6`; `--kv-cache fp32`
keeps it exact ([Precision](architecture.md#precision)).

**Evidence.** The one deliberate exception to "no rounding we do not benefit
from", chosen 2026-10-04 (#6830): it cuts KV memory about 3x (the expert tier
keeps about 1,400 more slots at 95k tokens, decode VRAM hits go from 26% to 57%,
decode +19%), and its effect on outputs is inside the model's own rounding
noise at long context (fp32 against fp32 with only the prefill chunk size
changed shifts next-token distributions as much), with retrieval and
long-context tasks unchanged. Lower bit widths are measurably lossier. A lossy
mode is judged by outcome (`oominf score`, retrieval, tasks), not by the
per-layer budgets.

**Tradeoffs.** Greedy output with `k8v6` varies run to run when experts run on
the CPU (a CPU rounding difference can flip a codebook index). Individual layers
sit above the per-layer bf16 budget in `check-model`; the chained logits gate
holds. Prefill keeps an exact fp32 shadow of the prefilling layer (about 0.4 GB
at 95k tokens).

**Revisit when.** `oominf score`, retrieval or a long-context task puts `k8v6`
outside the fp32 rounding band on a new workload or model, or on a GPU where KV
memory no longer limits the expert tier (the 1,400 slots stop mattering).

## D4: Speed modes off by default

**Context.** Four runtime choices trade output or placement for speed
([Precision](architecture.md#precision)).

**Decision.** `--dense fp8`, `--expert-precision bf16`,
`--attention-precision bf16` and `--kv-placement host` are all off by default.

**Evidence.**

- `--dense fp8`: half the bytes, so decode reads less (35-37 ms per verify step
  against 43-50 ms at 32k-95k) and the expert tier gains about 1,660 VRAM slots.
  Next-token distributions shift measurably (KL about 0.10-0.13, 86-88% top-1
  against the exact reference, about 3x the rounding floor, with per-row or
  per-block scales and with or without bf16 routers), while retrieval and the
  long-context task set still pass (#6865).
- `--expert-precision bf16`: the kernel then runs 2.2x faster and warm 32k
  prefill drops from 16.2 s to 14.0 s. Against the exact reference: KL 0.058 /
  92.1% top-1 at 32k and 0.035 / 91.2% at 95k, about at the rounding floor, and
  `check-model` stays inside the HF bf16 budget (#6838). Decode is unaffected.
- `--attention-precision bf16`: against the exact reference: KL 0.051 / 92.6%
  top-1 at 32k and 0.031 / 92.4% at 95k, inside the rounding floor. The kernel is
  bound by gathering each token's selected fp32 K/V rows, so it is only about 15%
  faster (1.51 to 1.27 s per 32k prefill).
- `--kv-placement host`: results are identical to device placement. It frees
  the cache's VRAM for experts: decode is 4% slower at 32k and 9% faster at
  95k, prefill 4-6% slower (each layer's fp32 shadow is read over PCIe) (#6856).
  With `--dense fp8` the expert tier already has the VRAM and host placement is
  slower at every length measured (32k-95k).

**Tradeoffs.** The defaults are slower; the max-perf config is what the
published speeds use. Users who accept the shift get it with three flags.

**Revisit when.** For `--expert-precision bf16` and `--attention-precision
bf16`, which already sit at or inside the rounding floor: a task-eval run
(`evals/`) shows no regression, which would justify defaulting them. For
`--dense fp8`: a scale scheme brings KL down to the rounding floor while keeping
the decode gain. For `--kv-placement host`: typical requests run near 95k
tokens without `--dense fp8`, where it measured faster.

## D5: An explicit pinned host tier

**Context.** Experts that miss VRAM come from host memory or disk on every
step.

**Decision.** The host tier is a pinned arena filled with direct (O_DIRECT)
reads, never the page cache ([Experts and tiers](architecture.md#experts-and-tiers)).

**Evidence.** On recorded decode traces the cache hit rate dominates decode time,
and on-disk layout barely matters because drives split large reads into small
commands anyway. An explicit pinned host tier makes memory use and copy timing
deterministic, where the page cache competes with everything else on the
machine.

**Tradeoffs.** Pinned memory is taken from the machine up front and sized at
start; the planner has to read container limits before pinning
([D10](#d10-size-from-fresh-checks-shrink-before-failing)). Filesystems without
O_DIRECT fall back to buffered reads that cost about 20% of prefill and a third
of decode ([measurements](measurements.md#simulated-machines-october-6-2026)).

**Revisit when.** Replaying recorded traces (`oominf-tiers/examples/replay.rs`)
shows the page cache, or a userspace pager, beating the pinned tier, for
example on a machine whose RAM holds every expert.

## D6: Host compute

**Context.** A record that misses VRAM but sits in the host tier must either
cross PCIe or be computed where it is.

**Decision.** In decode-sized steps, up to `--host-compute` (default 6) such
experts per layer run on the CPU; a repeat miss is copied to VRAM (second-hit
admission) ([A decode step](architecture.md#a-decode-step)).

**Evidence.** A host-tier hit costs a 2.7 MB copy over PCIe (about 110 us) that
the GPU waits for; the CPU computes the same expert for one token in about
70 us on 8 cores while the GPU runs the resident experts, and moves a 10 KB
row. On novel prompts warm decode drops about 7%; recurring experts still
reach VRAM through admission.

**Tradeoffs.** CPU and GPU round differently, so which experts run where makes
greedy output timing-dependent (use `--host-compute 0` for determinism). The
CPU threads are busy during decode.

**Revisit when.** The hardware profile already turns it off when one expert on
the CPU takes more than 2x a record's copy ([D11](#d11-the-profile-decides-only-two-things)).
Reconsider the default when a faster link (PCIe 5) makes the copy cheaper than
CPU compute on the reference CPU, or when workloads are mostly novel prompts
where the 7% loss applies.

## D7: Stage-ahead in prefill

**Context.** Prefill runs layer by layer and each layer needs most of its
experts.

**Decision.** The next layer's experts, predicted by its router, are staged
while the current layer computes ([Prefill](architecture.md#prefill)).

**Evidence.** Fetching a layer's experts only once it has routed left the GPU
idle for every layer's copies and disk reads: on a 2.2k-token prompt, copies
and compute overlapped for 0.3 s of a 4.7 s prefill. The prediction covers about
90% of the routed experts (93% of what it stages is used), so most of each
layer's loading now runs under the previous layer's compute; what is left is
bound by disk reads of records the host tier does not hold.

**Tradeoffs.** The stage borrows the main tier's coldest slots. Prompts of one
chunk skip it, because with little compute to hide copies behind the less
precise prediction costs more than it saves.

**Revisit when.** Another model or workload drops prediction coverage well below
the measured 90% (or the used fraction below 93%), or every expert fits in VRAM
so there is nothing to stage.

## D8: Staging copies trickle

**Context.** Stage-ahead copies and the computing layer's own fetch copies share
the GPU's copy engine.

**Decision.** Host-tier staging copies go to the copy engine a few records at a
time, the next only once those completed ([Prefill](architecture.md#prefill)).

**Evidence.** The GPU has one host-to-device copy engine and it runs ready copies
in submission order across streams, so stream priorities cannot reorder them.
Submitting a layer's stage-ahead at once (about 450 records, 1.2 GB, 45 ms of
engine time) made the computing layer's own fetch copies, needed now, wait
behind all of it: the profile of a warm 32k prefill showed the GPU idle for
2.7 s of 10.5 s, mostly in one stall per layer boundary. Sending the stage-ahead
in small pieces (8 records, about 1 ms of engine) as earlier ones complete
bounds that wait to one piece: warm prefill 10.30 to 9.40 s at 32k tokens and
28.1 to 27.4 s at 95k, decode unchanged. Disk volume was not the cause: a larger
host tier (19% fewer disk reads) did not change it.

**Tradeoffs.** The host polls and resubmits; the engine sees small batches.

**Revisit when.** A GPU or driver runs host-to-device copies by stream priority,
or offers a second copy engine for this direction, so a bulk submission no
longer blocks urgent copies.

## D9: No decode lookahead by default

**Context.** Reading predicted disk misses into the host tier during decode
looked free (`--lookahead`).

**Decision.** `--lookahead` defaults to `off` (commit `2ce6fe7b4`)
([A decode step](architecture.md#a-decode-step)).

**Evidence.** The reads it issues are for exactly the experts the cache does not
hold, where the prediction is weakest (62-65% precision over all predicted
experts, about 24% of lookahead reads then used), and a step predicts for every
token it carries, rejected drafts included. On the served 22k-token demo (warm,
three requests per setting), lookahead on against off: 45.4 against 48.4 tok/s
with no memory limit (20.6 against 20.8 demand disk reads per token: the used
reads saved none, since a placed guess counts as a fresh access and evicts
records that would have hit, plus 8.7 lookahead reads per token on top), and
16.5 against 21.2 tok/s at a 32 GiB limit, where the drive is the bottleneck
(74-78% of decode time waits on reads) and lookahead raised reads per token from
88 to 136, close to the drive's 7 GB/s. With prediction kept but no reads
issued, decode matched off (48.7 tok/s), so the cost is the reads, not the
bookkeeping (0.1-0.9% of decode time). `oominf bench` hides it: its workload
reads about 4 records per token from disk.

The simulated-machine runs (October 6, 2026) showed it first: with
`--lookahead off`, warm decode of the demo was 12.8 instead of 8.4 to 9.0 tok/s
at 16G, 20.2 instead of 15.7 at 32G, 39.3 instead of 33.0 at 48G, and 49.7
instead of 45.2 without a limit ([measurements](measurements.md#simulated-machines-october-6-2026)).

**Tradeoffs.** A workload whose next experts are predictable from the previous
layer loses whatever lookahead would have hidden.

**Revisit when.** A workload shows more than about half of lookahead reads used.
The way back: gate lookahead on the measured used fraction, insert guesses at
the cold end of the host tier, and skip draft tokens.

## D10: Size from fresh checks, shrink before failing

**Context.** Users run on smaller machines and in containers.

**Decision.** Tiers size themselves on every start from usable memory (container
limits included), shrink with a warning, lower default reserves before failing,
and fail only below the smallest working tiers with an itemised shortfall
([Smaller machines](architecture.md#smaller-machines)).

**Evidence.** A container limit is invisible to `MemAvailable`, and pinning past
it gets the process OOM-killed rather than an error back, so the limit has to be
read before anything is pinned. The memory beside the tiers grows with the
context, the stream count and the prefix store, and used to come out of the
fixed reserve unaccounted (the staging ring alone is 1.3 GiB); counting it makes
the reserve mean what it says, and 10 GiB with it counted leaves the same memory
free as 12 GiB did. Shrinking with a warning keeps a smaller machine serving at
lower speed instead of not at all; failing below the minimum, with the missing
amount, is the only case nothing can serve. Measured on the reference box under
container limits ([measurements](measurements.md#simulated-machines-october-6-2026)):
48, 32, 24, 20, 16, 12, 10 and 8 GiB all start and serve the 22k-token demo
correctly, with warm decode falling with the host tier (45, 33, 16, 12, 10,
9 tok/s down to 16 GiB; about 12.7 at the minimum, where lookahead is capped off)
and prefill at about 800 tok/s at the minimum (a one-record staging ring); 7 GiB
is refused at start-up with the itemised shortfall.

**Tradeoffs.** The default 10 GiB host reserve is conservative: at a 16 GiB
limit it was mostly unused (peak 4.6 GiB), and 4 GiB raised decode from 9.0 to
10.6 tok/s warm.

**Revisit when.** A real smaller machine (a slower disk, a GPU under 24 GB,
cgroup v1, or io_uring or O_DIRECT actually unavailable) fails or misbehaves
where the simulation served; the [not verified](measurements.md#not-verified)
list says which cases have only unit tests.

## D11: The profile decides only two things

**Context.** Defaults were tuned on one machine.

**Decision.** The hardware profile sets only host compute (off when the CPU is
more than 2x slower than a copy) and the prefill staging ring size, and warns
about slow drives and links ([Hardware profile](architecture.md#hardware-profile)).

**Evidence.** The defaults were tuned on one machine; the two choices above are
the ones whose right value follows directly from a hardware rate, and both are
conservative (they only turn off or shrink something that cannot pay off at
the measured rate). Everything else stays at measured defaults until there is
data from other machines.

**Tradeoffs.** Other machines may want other values (policies, reserves,
`--host-compute` counts) that the profile does not set.

**Revisit when.** `oominf doctor` and `oominf tune` output from other machines
shows a further default whose right value follows from a measured rate, or when
adapting while serving is built (hook: the batched engine's periodic report in
`oominf-server/src/engine/scheduler.rs`).

## D12: Prefix store and checkpoints

**Context.** Agents come back to long contexts minutes or hours later, and the
same documents get several questions.

**Decision.** Keep the live sequence on the device, save evicted sequences with
`--prefix-store-dir`, and keep prefix checkpoints during prefill
([Prefix store](architecture.md#prefix-store)).

**Evidence.** Restoring a 32k-token conversation took 0.9 s (5 s after a server
restart, with cold expert tiers) against 28 s of prefill. A new question on the
same 32k-token documents (max-perf config): first token after 0.64 s from the
live sequence's checkpoint and 0.95 s from a stored entry, against 13.1 s of
prefill (#6859).

**Tradeoffs.** Checkpoints take about 0.11 GB each in host memory and snapshots
in flight 0.75 GB each at 32k tokens, all counted against the host tier; the
store uses disk (default budget 200 GiB, 72 h time to live).

**Revisit when.** Restoring approaches prefill time (a slow disk, or much faster
prefill), or the host memory the checkpoints take measurably slows decode on
small machines.

## D13: MTP speculative decoding

**Context.** The checkpoint ships a multi-token-prediction head.

**Decision.** Draft one token with the MTP head (`--draft 1`) and verify it in
the next step ([Speculative decoding](architecture.md#speculative-decoding)).

**Evidence.** Dense weights are read once per step whatever its width, so
verifying a draft costs much less than a second step. The gain is bounded by the
draft acceptance rate (55 to 75% measured) and by the extra routed experts the
drafted token pulls in.

**Tradeoffs.** Rejected drafts must be rewound (GDN and PLE replay), and greedy
output can differ from one-token decoding at near ties.

**Revisit when.** Acceptance on a workload falls below the measured 55%, or a
verify step's extra routed experts cost more than the kept tokens save.

## D14: Prompt lookup on by default

**Context.** Coding agents print edited files whole, repeat code and quote
input, so long spans of output already exist in the sequence.

**Decision.** `--prompt-lookup 7` is on by default (#6872).

**Evidence.** Measured end to end (`bench/lookup.py`, medians of three warm
runs, max-perf config): +22 to +40% output rate on file edits (92 to 95% of
lookup drafts kept, about 7 tokens per step), level on tests, -2 to -9% on prose,
quotes and a short diff. An 8-token verification step costs 110 to 125 ms when
its tokens are new (about 2,200 routed records, a quarter outside VRAM) against
about 25 ms for one token, so it pays only when most of a draft is kept; matches
shorter than 4 tokens drafted novel text often enough to lose. Before the
decode-kernel paths, an 8-token step fell onto the sparse prefill attention and
dequantized FP8 weights for cuBLAS every step (215 ms).

**Tradeoffs.** Prose and short diffs lose 2 to 9%.

**Revisit when.** A deployment's traffic is mostly prose, where it measured a
loss; then default it off there (`--prompt-lookup 0`) or make it adaptive.

## D15: Keep residuals across lookup steps

**Context.** Steps drafted by prompt lookup do not call the draft head, so it
falls behind.

**Decision.** Keep the final residuals of the last 256 positions in a ring and
catch the head up lazily at the next model draft
([Speculative decoding](architecture.md#speculative-decoding)).

**Evidence.** The ring used to hold only the last step's rows, so two lookup
steps in a row made the head restart its cache and draft without the output's
context. On the blog demo (a 22k-token incident report in, about 580 tokens of
newline-delimited JSON out, sampled, serve defaults, warm) model drafts kept 76%
with lookup on against 83% with it off, and decode ran at 43.4 against 47.0
tok/s (medians of 12 and 4 requests). Catching up restores 83% acceptance and
45.6 tok/s with lookup on (median of 10 requests; lookup off unchanged within
noise, 46.1 tok/s). The file-edit workloads of `bench/lookup.py` keep their
lookup gains: within -4 to +2% of before on every workload (medians of five
warm passes, which vary by a few percent between server runs). The cost is the
catch-up itself, one MTP layer step per four positions, which used to be
skipped: rings of 32, 64 and 256 rows measured the same on the edit workloads.
Catching up lazily at the next model draft is never more work than catching up
eagerly after every lookup step (the same positions, in fuller steps), so it is
kept. `tests/mtp_lookup.rs` checks that drafts after lookup runs match drafts
made on every step (it tolerates one near-tie difference in 20 draft tokens):
measured 0 of 48 tokens differ, against 13 when the head restarts.

Rejected alternative: a draft width that grows from one to three tokens while
recent model drafts were mostly kept (90% for two, 95% for three) was measured
and not kept: on the demo it tied one-token drafts (46.4 against 46.3 tok/s) and
it lost 3 to 7% on the edit workloads, whose high acceptance comes from easy
positions lookup leaves the head. Fixed two- and three-token drafts ran the demo
at 45.6 and 42.8 tok/s.

**Tradeoffs.** 10 MB per sequence and one MTP layer step per four caught-up
positions.

**Revisit when.** The catch-up cost grows (a heavier draft head), or a variable
draft width beats one-token drafts on both the demo and the edit workloads.

## D16: One batched step

**Context.** Several requests decode at once (`--max-streams`).

**Decision.** One step carries every decoding stream's tokens; row-wise work
runs once over all rows and each layer fetches the union of their experts once
([Concurrent requests](architecture.md#concurrent-requests)).

**Evidence.** Decode is bound by routed-expert misses, and a step fetches the
union of its rows' experts once per layer, so rows of different sequences share
the misses their experts have in common and every dense weight is read once.
Measured with `oominf bench --streams` (max-perf config, warm tiers, 16
different requests, one token per sequence per step): 1 sequence 22 ms per step
(45 tok/s), 2 sequences 37 ms (54 tok/s), 4 sequences 52-55 ms (73-77 tok/s),
8 sequences 115-120 ms (67-70 tok/s), 16 sequences 254-257 ms (62-63 tok/s). The
plateau comes early because unrelated requests share few experts: 8 sequences
route about as many records per step as 8 verified drafts of one sequence
(2,266 against 2,317), but across steps their working set outgrows the VRAM tier
(hit rate 68% against 84%), so host-tier copies dominate. Running each
sequence's token mixer on its own first cost about 10 ms per extra sequence (its
dense GEMVs and about 25 kernel launches per layer); batching every row-wise
operation removed that (8 sequences 146 to 120 ms per step).

Measured through the server (`bench/http_bench.py --concurrency`, K concurrent
streaming requests of 128 tokens with different short prompts, temperature 0,
the same max-perf config; median of two rounds):

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

**Tradeoffs.** Each stream is slower when others decode (about 31 tok/s each
for two against about 43 for one, per the `--max-streams` help); the default of
2 lets a second request start without waiting for the first.

**Revisit when.** Many streams stop thrashing the VRAM tier (a larger GPU, or
traffic whose requests share experts), so that throughput keeps rising past 4
streams.

## D17: Prefills not sliced by default

**Context.** A new request's prefill stalls the other streams' decoding.

**Decision.** `--prefill-slice 0`: prompts prefill whole
([Concurrent requests](architecture.md#concurrent-requests)).

**Evidence.** A prefill of any size above a few hundred tokens touches most of
every layer's experts, so its cost is about one sweep of the expert tiers
whatever its length. Slicing a 2k-token prompt between other streams' steps (4
concurrent requests, one of them long) cut the others' longest stall from 3.6 s
to 2.2 s (512-token slices) or 2.7 s (1024) but raised the long request's time
to first token from 3.5 s to 9.8 s or 6.9 s and lowered aggregate throughput
from 38 to 32-33 tok/s.

**Tradeoffs.** Other streams stall for a whole prefill. `--prefill-slice` is
there for workloads that prefer the shorter stall.

**Revisit when.** Decoding streams' rows can ride along inside the layer-major
prefill, which fetches every layer's experts anyway; that is the way to a stall
shorter than a sweep.

## D18: Fixed references and checked protocols

**Context.** Numerical drift and use-after-overwrite bugs both produce output
that looks plausible.

**Decision.** Every change is checked against the model's reference
implementation ([testing.md](testing.md)), and protocols with concurrency are
specified in TLA+ and model-checked ([`specs/`](../../specs/README.md)).

**Evidence.** Fixed references and checked protocols make both kinds of bug fail
loudly. The tier fault-injection tests found four defects, each fixed with the
test that exposed it ([testing.md](testing.md#tier-fault-injection)).

**Tradeoffs.** Fixtures need the release checkpoint and a CPU run of the
reference code; the GPU gates take the machine's GPU.

**Revisit when.** A new concurrent protocol lands without a spec (the prefix
cache and the KV and scheduler allocation are listed as planned in
[`specs/`](../../specs/README.md)).
