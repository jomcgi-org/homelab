# Architecture

oom-inference serves Mixture-of-Experts models that are larger than GPU memory
plus host memory, on one consumer machine, for a few interactive streams.
Routed experts live in three tiers (VRAM, pinned host memory, NVMe) and move on
demand; everything else stays resident on the device.

## Crates

| Crate | Role | Depends on |
|---|---|---|
| `oominf-core` | Platform- and model-neutral interfaces: `Backend`, `Model`/`Session`, `ExpertSource`, `Workspace`, `Probe` | nothing in the workspace |
| `oominf-format` | On-disk weight format: reader, writer, checksums (`FORMAT.md`) | |
| `oominf-convert` | Release checkpoint (safetensors) to the format, one adapter per model family | format |
| `oominf-cuda` | CUDA implementation of `Backend`: device memory, streams, events, cuBLAS, kernels in `kernels/*.cu` | core |
| `oominf-metal` | Apple Silicon operations, shared buffers and ordered commands | core, tiers |
| `oominf-cpu` | Routed experts computed on the host from records in host memory (exact NVFP4 and bf16 decoding, fp32, AVX-512 with a scalar path), on a worker pool | core |
| `oominf-tiers` | Expert sources: `TieredExperts` (VRAM slots, pinned host arena filled by direct I/O, cache policies) and `DiskExperts` | core, format |
| `oominf-models-qwen` | Qwen 3.8 Flash, generic over `B: Backend` | core, cpu, format |
| `oominf-models` | Registry: opens a converted model with the implementation for its `model_type` | core, format, models-* |
| `oominf-server` | OpenAI and Anthropic HTTP APIs, chat templates, output parsers, sampling, prefix reuse, continuous batching | core |
| `oominf` | CLI and composition root: picks the backend, the model (via the registry) and the expert source | all |

Only `oominf-cuda` knows about CUDA. Only `oominf-models-*`, the converter's
adapter and the server's output parser know about a model family. The server
runs any `Model`; the CLI is the one place that names the CUDA backend.

**Why.** New hardware and new models are the two expected kinds of growth. A
platform port implements `Backend` and reuses every model, the tiers and the
server; a new model family reuses the backend, the tiers and the server. Neither
touches the other.

### Apple Silicon direction

Direct Metal is the selected backend for the 16 GiB M1 Pro port. The initial
checkpoint is Qwen3.5-35B-A3B in its released ModelOpt NVFP4 format. Conversion
keeps the text decoder and routed experts, excluding vision and the optional
MTP draft head. Both sharded and single-file safetensors checkpoints are accepted.

**Why.** Expert streaming and bounded memory are central to this engine. On
Apple Silicon, CPU buffers and Metal buffers consume the same physical RAM, so
the composition root must budget resident weights, sequence state, expert cache
and staging together. Direct Metal keeps those allocations and command ordering
under the engine's control. MLX's implementations remain a reference for model
semantics and kernel techniques; the choice does not establish a speed advantage
over MLX.

`oominf-models-qwen35` implements the complete text decoder using Metal
operations through `Model` and `Session`. The Mac CLI selects it directly;
CUDA model loading remains on Linux. Thirty gated delta layers carry fp32
recurrent state, and ten full attention layers keep fp32 K/V. Partial RoPE,
head gates, shared experts and released BF16 normalization offsets are preserved.
Matrix operations accumulate in fp32 with fast math disabled; NVFP4 weights and
E4M3 scales are decoded directly. The independent MLX reference reads original
safetensors and compares all vocabulary logits across successive tokens.

Shared copies are synchronous and ordered after compute. Disk reads use
`F_NOCACHE` pread workers; Linux retains io_uring and O_DIRECT. The allocator
limits Metal allocations to current usable RAM less a reserve, capped at the
device's recommended working set. The composition root reserves host staging,
sequence state and transient activations before allocating the expert cache.
It releases expert cache chunks when additional sessions need memory.

**Why.** Embedding rows are paged from `dense.bin`, spending 4 KiB per token
instead of keeping the 1 GB vocabulary table resident. The output head remains
resident because every generated token scores the whole vocabulary.

| Direction | Issue |
|---|---|
| Implement and validate Qwen3.5 text inference on direct Metal, then measure actual cold/warm performance and combined RAM use | [#6896](https://github.com/jomcgi-org/homelab/issues/6896) |

## Interfaces

- **`Backend`** (`oominf-core/src/backend.rs`) is a bundle of traits named for
  what models need: `Memory` (typed device buffers, host transfers, raw
  addresses), `Linear` (GEMM/GEMV with fp32 accumulation), `Elementwise`, `Norm`,
  `HyperConnection`, `Recurrent` (convolutions and the gated delta rule with
  carried state), `Attention` (rotary, block-sparse selection, grouped-query
  attention), `Experts` (routing and fused routed-expert compute on quantised
  records read in place), `Lookup` (quantised embedding rows) and `Transfer`
  (a copy queue that runs alongside compute, pinned host memory, events). Model
  code is generic over `B: Backend` and monomorphised: no dynamic dispatch inside
  a step.
- **`Model`** opens sessions; a **`Session`** owns one sequence's state (caches,
  recurrent state) and runs `prefill(tokens)` and `step(tokens)`, returning the
  logits after the last token. For speculative decoding a session may also
  `draft(next, k)` tokens, verify them in one `step_all`, and `rewind` the ones
  the model rejects. `Model::step_many` runs one step over several sessions
  (continuous batching, below). The server, `generate` and `bench` use only
  these.
- **`ExpertSource`** makes a layer's routed experts device-resident and returns
  their record addresses, optionally in two phases so resident experts compute
  while the rest load. Kernels read every part of a record, scales included,
  from that address. An `ExpertFactory` builds the source after the model's
  dense weights and first session state are on the device, so tiers size
  themselves from what is left.
- **`Probe`** observes (and in tests substitutes) named intermediate tensors;
  production passes `NoProbe`.

## Precision

Weights are used exactly as released. Runtime precision is reduced only where
the hardware gains from it, and any such mode is opt-in and must pass the gates
in `TESTING.md`. Defaults:

- NVFP4 experts: weights decoded exactly in fp32 (`e2m1 * fp8 * scale_2`),
  activations unquantised (W4A16), fp32 accumulation. Prefill runs them on
  tensor cores without giving that up: `e2m1 * fp8` is an exact bf16 value
  (`scale_2` scales the fp32 result), each fp32 activation splits exactly into
  three bf16 terms, and the products accumulate in fp32.
- Dense weights: bf16 as released. Decode GEMVs read fp32 activations directly;
  prefill GEMMs round activations to bf16 for tensor cores.
- Residual stream, norms, softmax and recurrent state: fp32.
- MTP experts (the draft head): bf16 as released, fp32 activations and
  accumulation.
- KV cache: compressed by default, `k8v6` (`KvFormat::Turbo`: TurboQuant-style
  rotation, then 8-bit keys and 6-bit values from Lloyd-Max codebooks with a
  scale per 32 coordinates); `--kv-cache fp32` keeps it exact. The indexer caches
  stay fp32.

**Why k8v6 by default.** The one deliberate exception to "no rounding we do not
benefit from", chosen 2026-10-04 (#6830): it cuts KV memory about 3x (the
expert tier keeps about 1,400 more slots at 95k tokens, decode VRAM hits go from
26% to 57%, decode +19%), and its effect on outputs is inside the model's own
rounding noise at long context (fp32 against fp32 with only the prefill chunk
size changed shifts next-token distributions as much), with retrieval and
long-context tasks unchanged. Lower bit widths are measurably lossier. A lossy
mode is judged by outcome (`oominf score`, retrieval, tasks), not by the
per-layer budgets. Prefill keeps an exact fp32 shadow of the prefilling
layer's keys and values (one layer at a time, about 0.4 GB at 95k tokens), so
prefill attention reads fp32 rows instead of re-decoding cached tiles in every
block and runs at fp32 speed; decode reads the compressed cache.

Four more runtime choices trade differently:

- `--dense fp8` stores the dense (non-expert) weights as FP8 e4m3, one scale per
  128 weights, quantized on the host at load (routers stay bf16): half the bytes,
  so decode reads less (35-37 ms per verify step against 43-50 ms at 32k-95k) and
  the expert tier gains about 1,660 VRAM slots. Next-token distributions shift
  measurably (KL about 0.10-0.13, 86-88% top-1 against the exact reference, about
  3x the rounding floor, with per-row or per-block scales and with or without bf16
  routers), while retrieval and the long-context task set still pass (#6865).
  Prefill dequantizes each weight to bf16 once per layer (a 512 MB cache freed
  when the prefill ends). Off by default.
- `--expert-precision bf16` rounds the prefill expert GEMM's activations to bf16
  (one tensor-core pass, standard W4A16) instead of splitting them into three
  exact bf16 terms. The kernel then runs 2.2x faster and warm 32k prefill drops
  from 16.2 s to 14.0 s. Against the exact reference: KL 0.058 / 92.1% top-1 at
  32k and 0.035 / 91.2% at 95k, about at the rounding floor, and `check-model`
  stays inside the HF bf16 budget (#6838). Decode is unaffected. Off by default.
- `--attention-precision bf16` runs prefill attention on bf16 tensor cores (queries,
  keys and values rounded to bf16, fp32 accumulation and softmax) instead of fp32
  FMAs; decode attention stays fp32. Against the exact reference: KL 0.051 / 92.6%
  top-1 at 32k and 0.031 / 92.4% at 95k, inside the rounding floor. The kernel is
  bound by gathering each token's selected fp32 K/V rows, so it is only about 15%
  faster (1.51 to 1.27 s per 32k prefill). Off by default.
- `--kv-placement host` keeps attention K/V caches in host memory the GPU reads
  over PCIe (managed memory preferring the host); the indexer's keys stay on the
  device, so decode reads only each token's selected rows. Results are identical
  to device placement. It frees the cache's VRAM for experts: decode is 4%
  slower at 32k and 9% faster at 95k, prefill 4-6% slower (each layer's fp32
  shadow is read over PCIe) (#6856). With `--dense fp8` the expert tier already
  has the VRAM and host placement is slower at every length measured (32k-95k).
  Off by default.

Attention follows each query's QSA selection (at most 2,048 keys): decode splits
one token's list over warps, and prefill runs one block per (token, KV head)
over that token's own list. Grouping tokens to share key tiles does not pay:
adjacent tokens share few selected keys (a key in a 4-token group's union is
seen by about 1.2 of them), so shared tiles mostly compute masked-out scores.

Reproducibility: experts computed on the CPU round differently from the GPU, and
which ones run there depends on cache timing. With an exact cache that rarely
changes a token; a compressed cache can turn it into a different codebook index,
so greedy output with `k8v6` varies run to run. With `--host-compute 0` it is
deterministic, and speculative decoding again matches one-token decoding up to
near ties (below).

**Why.** The engine exists to run a frontier model on modest hardware without
making it worse. Rounding that buys nothing is pure loss, and silent precision
trades are how engines drift from the model they claim to run.

## Experts and tiers

- A converted model stores one contiguous record per (layer, expert)
  (`FORMAT.md`), so a miss is one read and one copy.
- **VRAM tier:** record-sized slots in chunked arenas, so whole chunks can be
  given back when a growing KV cache needs memory and taken back for the next
  sequence. **Host tier:** a pinned arena filled with direct (O_DIRECT, io_uring)
  reads, never the page cache, or through the fallbacks of
  [Smaller machines](#smaller-machines) where those are unavailable. **Disk:** the model files.
- Placement is a `Policy` (LRU and decay-weighted frequency), chosen by replaying
  recorded routing traces (`oominf-tiers/examples/replay.rs`).
- **Host compute** (decode-sized steps): a record that misses VRAM but is in the
  host tier can stay there and run on the CPU (up to `--host-compute` experts
  per layer), overlapping the GPU's resident experts; only its output rows move.
  Admission to VRAM is second-hit: a key's first host-tier miss within a window
  runs on the CPU, a repeat is copied to VRAM, so one-off experts do not evict
  recurring ones.
- Prefill streams through a one-layer stage instead of evicting decode-hot
  experts. It runs layer by layer: a layer's token mixer runs over every chunk,
  the union of their routed experts is fetched once and runs as one step (up to
  2048 tokens, about 40 per expert, enough for tensor-core tiles), and the next
  layer's experts, predicted by its router on each group (stacked with this
  layer's router, so the prediction arrives with the routing; the first group
  opens the batch, later groups add what their tokens route to), are staged
  while the rest of this layer computes (stage-ahead), borrowing the main
  tier's coldest slots while the stage holds the computing layer. Staging copies
  wait only for kernels queued before the computing layer started (only those
  read the slots they overwrite), and their disk reads land as they complete.
  Host-tier staging copies go to the copy engine a few records at a time (the
  next only once those completed) while the host polls for routing, so a group's
  own copies never queue behind the next layer's.
  The host runs one group ahead of the device: a group's mixers and routing are
  queued before the previous group's routing is collected (an asynchronous
  download) and its experts fetched, and small uploads go through pinned
  staging buffers, so neither drains the device's queue. Prompts of one
  chunk skip this: with little compute per layer to hide copies behind, the less
  precise prediction costs more than it saves. Prompts longer than 128k tokens,
  or whose residuals (about 40 KB per token, held across layers) do not fit
  beside the prompt's cache and the expert tier's floor, run in the fewest equal
  windows that fit, each window through every layer. Each extra window sweeps
  every layer's experts through VRAM again (about 4.5 s on a 4090). With
  61 GB of RAM the host tier holds about 40 GB of the 73 GB of experts, so a
  warm prefill still reads about 19 GB from disk; in the later layers those reads
  outlast a layer's compute (#6866). Between prefills the stage is a
  decode victim cache: an evicted record is copied there device to device, so a
  later miss on it is a promotion. When a prefill ends, its prefill-only buffers
  (residuals, KV shadows, fetch-group buffers) are freed and the memory is
  offered back to the expert tier for decode.
- With `--lookahead on` (off by default), during decode (and draft verification)
  layer L+1's router applied to layer L's input predicts the next experts, and
  predicted disk misses are read into the host tier. Routing, not prediction,
  decides which experts run.
- Groups with different record layouts (the decoder layers' NVFP4 experts, the
  MTP layer's bf16 experts) get separate tiers behind one source. A small group
  gets one VRAM chunk and has its records read into the host tier at start-up:
  measured on the MTP experts, more device slots barely shorten drafting while
  every slot taken from the main tiers costs decode hits.

## Smaller machines

The tiers size themselves on every start from fresh checks, shrink what does not
fit and fail only when the smallest working configuration does not fit
(`oominf-tiers/src/plan.rs`, `resources.rs`; `oominf doctor` prints the result
without starting).

- **Usable host memory** is the smaller of `MemAvailable` and the control
  group's headroom: cgroup v2 `memory.max` or `memory.high`, whichever is lower,
  less the group's use minus its inactive page cache, taking the tightest
  ancestor; cgroup v1 `memory.limit_in_bytes` the same way.
- **Host split.** Usable memory = reserve (`--host-reserve-gib`, default 10) +
  memory beside the tiers + the tiers. Beside the tiers, at the configured
  context and stream count (`HostDemand` from the model, `HostUse` from the
  command): host-placed KV caches per live sequence (`--kv-placement host`),
  prefix checkpoints per live sequence (the planned count at `--max-context`,
  about 0.12 GB each), three prefix-store snapshots when a store is set (one
  being built, one queued, one being written; 0.75 GB each at 32k tokens), and
  a 0.5 GiB allowance for pinned transfer and work buffers. The prefill staging
  ring is part of the tiers' budget.
- **VRAM split** is the same over free VRAM once the model and a first sequence
  are loaded, with `--vram-reserve-gib` (default 2).
- **Shrinking.** A requested size that does not fit is shrunk to what does, with
  a warning naming the figures. No tier is larger than the records its layout
  has. When the result is below the smallest working tiers, a default reserve is
  lowered as far as 3 GiB (host) or 1 GiB (VRAM), keeping as much reserve as
  possible; an explicit reserve is kept. Only then does start-up fail, with the
  needed total itemised.
- **Smallest working tiers** (`min_vram_slots`, `min_host_slots`): VRAM holds the
  largest fetch that does not stream, in 64-record chunks (one chunk beside a
  stage; a whole layer, 512 records, without one); the host tier holds that
  fetch's records too (64 with a stage). For Qwen 3.8 Flash that is 0.2 GiB of
  host tier plus a one-slot ring with a stage, or 1.4 GiB without; the MTP layer's
  minor tier adds 0.6 GiB. Lookahead never pins more host slots than those left
  beyond the minimum, and a fetch that still finds every slot pinned waits for
  the lookahead reads to land.
- **Allocation.** Pinned arenas retry at three quarters of the size down to the
  minimum when mapping or pinning fails. VRAM chunks are kept as far as they
  allocate; the stage is dropped before the main tier would fall below its
  minimum.
- **Reads.** `experts.bin` is read with O_DIRECT through io_uring when both work;
  else O_DIRECT `pread` on a thread pool (io_uring blocked by a seccomp profile or
  missing); else buffered `pread` on a thread pool that drops the read pages from
  the page cache (a filesystem without O_DIRECT). Each start tries a real read on
  each path and logs which one is active; `--io` forces one.

**Why.** A container limit is invisible to `MemAvailable`, and pinning past it
gets the process OOM-killed rather than an error back, so the limit has to be
read before anything is pinned. The memory beside the tiers grows with the
context, the stream count and the prefix store, and used to come out of the
fixed reserve unaccounted (the staging ring alone is 1.3 GiB); counting it makes
the reserve mean what it says, and 10 GiB with it counted leaves the same memory
free as 12 GiB did. Shrinking with a warning keeps a smaller machine serving at
lower speed instead of not at all; failing below the minimum, with the missing
amount, is the only case nothing can serve. Measured on the reference box under
container limits ([HARDWARE.md](HARDWARE.md)): 48, 32, 24, 20, 16, 12, 10 and
8 GiB all start and serve the 22k-token demo correctly, with warm decode falling
with the host tier (45, 33, 16, 12, 10, 9 tok/s down to 16 GiB; about 12.7 at the
minimum, where lookahead is capped off) and prefill at about 800 tok/s at the
minimum (a one-record staging ring); 7 GiB is refused at start-up with the
itemised shortfall.

## Hardware profile

`serve` measures the machine just before the model loads when no cached
profile matches (`oominf/src/profile.rs`; about a second on the reference
NVMe): scattered and consecutive record reads of `experts.bin` through the
active read path, single-record latency, pinned host-to-device copies, a
one-thread memory copy and the host expert kernel (six experts of a one-token
step on real records). The profile is cached in `$XDG_CACHE_HOME/oominf/` (or
`~/.cache/oominf/`) keyed by GPU name, VRAM, driver, CPU model, usable CPUs
(affinity, so `taskset` counts), RAM, the model's block device and filesystem,
and the engine and probe version; any change measures again. `--no-probe`,
`--reprobe` and `--profile <file>` control it; `oominf tune` measures four times
longer (median of three rounds) and writes it. Safety checks are never cached.

The profile decides only:

- **Host compute off** (`--host-compute 0` by default) when one expert on the CPU
  takes more than 2x a record's PCIe copy: the GPU would wait for the CPU. On the
  reference machine an expert takes 57 us on 8 threads against 103 us to copy its
  2.8 MB record at 26.8 GB/s, so host compute stays on.
- **Prefill staging ring** no larger than the drive fills in 0.5 s (about two
  layers of a long prefill's compute), at least 64 records: slots beyond that
  wait idle, and the memory goes to the host tier. The reference drive (6.9 GB/s)
  fills a whole layer, so the ring stays at 512 records.
- **Warnings** when scattered reads run under 1 GB/s (cold prefill and decode
  misses), reading the dense weights would take over 60 s, or host-to-device
  copies run under 6 GB/s.

A flag on the command line always wins over the profile. Adapting these while
serving (from measured steps, as the step cost curve already is) is not done;
it would hook in at the batched engine's periodic report
(`oominf-server/src/engine/scheduler.rs`).

**Why.** The defaults were tuned on one machine; the two choices above are the
ones whose right value follows directly from a hardware rate, and both are
conservative (they only turn off or shrink something that cannot pay off at
the measured rate). Everything else stays at measured defaults until there is
data from other machines.

## Prefix store

The server keeps the last sequence on the device and resumes a request that
extends it (multi-turn chat, agent loops). With `--prefix-store-dir`, a sequence
evicted from the device (a request that does not extend it, or shutdown) is saved
to disk (`Session::save`: per GDN layer its recurrent and conv state, per
attention layer its cached K/V rows, indexer and block keys, per PLE layer its
conv state and n-gram tokens, the draft head's input row, plus the last logits).
A later request whose prompt extends a saved sequence restores it into a fresh
session instead of prefilling. Entries are verified against their exact token
ids, the checkpoint and KV-format identity and a checksum; the directory is
bounded by a byte budget (least recently used first) and a time to live; host
memory caching comes from the page cache. Saving copies the state to the host on
the engine thread and writes the file on a writer thread.

**Prefix checkpoints.** The GDN layers' recurrent state cannot rewind, so a
sequence alone can only serve prompts that extend all of its tokens. To serve the
same documents with a different question, a prefill keeps checkpoints: the
server plans positions at the starts of the first and last user messages (found
by rendering the template with a marker in that message and matching the token
prefix) and at powers of two from 8k tokens. Layer-major prefill ends a chunk at
each one and copies every GDN and PLE layer's recurrent state on the device as
the layer passes it (about 0.11 GB per checkpoint, counted in the prefill's
memory), then brings the copies to host memory when the prefill ends
(`crates/oominf-models-qwen/src/checkpoint.rs`). A prompt that shares only a
prefix with the live sequence or a stored entry resumes at the longest
checkpoint inside that prefix (`Session::rewind_to`: attention caches are
truncated, recurrent state restored) and prefills only the rest; entries save
their checkpoints with them. Rewinding is exact: a rewound sequence fed a new
continuation matches, bit for bit, one that prefilled just the prefix and then
the continuation (`tests/checkpoint.rs`).

**Why.** Agents come back to long contexts minutes or hours later, and the same
documents get several questions. Restoring a 32k-token conversation took 0.9 s
(5 s after a server restart, with cold expert tiers) against 28 s of prefill. A
new question on the same 32k-token documents (max-perf config): first token
after 0.64 s from the live sequence's checkpoint and 0.95 s from a stored entry,
against 13.1 s of prefill (#6859).

## Speculative decoding

The checkpoint's multi-token-prediction head drafts the next token from the last
token's final residual; one step then feeds the current token and the drafts,
and `decode_step` (`oominf-core/src/decode.rs`) keeps the longest prefix the
model agrees with, plus the model's own next token. Greedy decoding keeps a
draft only when it is the argmax, so output matches one-token decoding except
where a verify step's different summation order flips a near tie (the
`speculative` test accepts a divergence only where the one-token run's top two
logits are within 0.05, and reports each); sampling
keeps it with the model's probability of it and otherwise samples the model
without it, so output keeps the model's distribution. Rejected drafts are
rewound: GDN and PLE layers restore the step's starting state and replay their
recurrences over the kept rows; attention layers cut their KV length.

**Why.** Dense weights are read once per step whatever its width, so verifying a
draft costs much less than a second step. The gain is bounded by the draft
acceptance rate (55 to 75% measured) and by the extra routed experts the
drafted token pulls in.

Prompt lookup (`PromptLookup`, `serve --prompt-lookup 7`, on by default) drafts
from the sequence itself before asking the model: when the last 8, 6 or 4 tokens
(longest first) occur earlier in the prompt or output, the 7 tokens that followed
their most recent occurrence are verified in one step; otherwise the model
drafts as above. Verification steps of 5 to 16 tokens run on the decode kernels
(GEMVs instantiated for 8 and 16 tokens, FP8 in launches of 8; per-token flash
decode attention), not the prefill ones.

**Why prompt lookup.** Coding agents print edited files whole, repeat code and
quote input, so long spans of output already exist in the sequence. Measured
end to end (`bench/lookup.py`, medians of three warm runs, max-perf config):
+22 to +40% output rate on file edits (92 to 95% of lookup drafts kept, about 7
tokens per step), level on tests, -2 to -9% on prose, quotes and a short diff.
An 8-token verification step costs 110 to 125 ms when its tokens are new (about
2,200 routed records, a quarter outside VRAM) against about 25 ms for one token,
so it pays only when most of a draft is kept; matches shorter than 4 tokens
drafted novel text often enough to lose. Before the decode-kernel paths, an
8-token step fell onto the sparse prefill attention and dequantized FP8 weights
for cuBLAS every step (215 ms).

The draft head keeps its own attention cache across calls: entry `p` fuses the
final residual at position `p` with the embedding of the token at `p + 1`, and
each call first appends the entries for the tokens kept since the last call.
Steps drafted by prompt lookup do not call the head, so it falls behind by every
token they keep. The sequence therefore keeps the final residuals of its last
256 positions in a ring (40 KB each, 10 MB) with the tokens that follow them,
and the next model draft catches the head up on every position since its last
call, in steps of four. Only after more than 256 lookup-kept tokens in a row
does the head restart its cache a few positions back. A rewind cuts the ring's
token list; rows of dropped positions are overwritten later.

**Why keep residuals across lookup steps.** The ring used to hold only the last
step's rows, so two lookup steps in a row made the head restart its cache and
draft without the output's context. On the blog demo (a 22k-token incident
report in, about 580 tokens of newline-delimited JSON out, sampled, serve
defaults, warm) model drafts kept 76% with lookup on against 83% with it off,
and decode ran at 43.4 against 47.0 tok/s (medians of 12 and 4 requests).
Catching up restores 83% acceptance and 45.6 tok/s with lookup on (median of
10 requests; lookup off unchanged within noise, 46.1 tok/s). The file-edit
workloads of `bench/lookup.py` keep their lookup gains: within -4 to +2% of
before on every workload (medians of five warm passes, which vary by a few
percent between server runs). The cost is the catch-up itself, one MTP layer
step per four positions, which used to be skipped: rings of 32, 64 and 256
rows measured the same on the edit workloads. Catching up lazily at the next
model draft is never more work than catching up eagerly after every lookup
step (the same positions, in fuller steps), so it is kept.
`tests/mtp_lookup.rs` checks that drafts after lookup runs match drafts made on
every step (it tolerates one near-tie difference in 20 draft tokens): measured
0 of 48 tokens differ, against 13 when the head restarts.

A draft width that grows from one to three tokens while recent model drafts
were mostly kept (90% for two, 95% for three) was measured and not kept: on the
demo it tied one-token drafts (46.4 against 46.3 tok/s) and it lost 3 to 7% on
the edit workloads, whose high acceptance comes from easy positions lookup
leaves the head. Fixed two- and three-token drafts ran the demo at 45.6 and
42.8 tok/s.

**Why stage-ahead.** Fetching a layer's experts only once it has routed left
the GPU idle for every layer's copies and disk reads: on a 2.2k-token prompt,
copies and compute overlapped for 0.3 s of a 4.7 s prefill. The prediction
covers about 90% of the routed experts (93% of what it stages is used), so most
of each layer's loading now runs under the previous layer's compute; what is
left is bound by disk reads of records the host tier does not hold.

**Why staging copies trickle.** The GPU has one host-to-device copy engine and
it runs ready copies in submission order across streams, so stream priorities
cannot reorder them. Submitting a layer's stage-ahead at once (about 450
records, 1.2 GB, 45 ms of engine time) made the computing layer's own fetch
copies, needed now, wait behind all of it: the profile of a warm 32k prefill
showed the GPU idle for 2.7 s of 10.5 s, mostly in one stall per layer
boundary. Sending the stage-ahead in small pieces (8 records, about 1 ms of engine) as
earlier ones complete bounds that wait to one piece: warm prefill 10.30 to 9.40 s at
32k tokens and 28.1 to 27.4 s at 95k, decode unchanged. Disk volume was not
the cause: a larger host tier (19% fewer disk reads) did not change it.

**Why no decode lookahead.** Reading predicted disk misses ahead looked free, but
the reads it issues are for exactly the experts the cache does not hold, where the
prediction is weakest (62-65% precision over all predicted experts, about 24% of
lookahead reads then used), and a step predicts for every token it carries,
rejected drafts included. On the served 22k-token demo (warm, three requests per
setting), lookahead on against off: 45.4 against 48.4 tok/s with no memory limit
(20.6 against 20.8 demand disk reads per token: the used reads saved none, since a
placed guess counts as a fresh access and evicts records that would have hit, plus
8.7 lookahead reads per token on top), and 16.5 against 21.2 tok/s at a 32 GiB
limit, where the drive is the bottleneck (74-78% of decode time waits on reads)
and lookahead raised reads per token from 88 to 136, close to the drive's 7 GB/s.
With prediction kept but no reads issued, decode matched off (48.7 tok/s), so the
cost is the reads, not the bookkeeping (0.1-0.9% of decode time). `oominf bench`
hides it: its workload reads about 4 records per token from disk. A gate on the
measured used fraction, inserting guesses at the cold end of the host tier and
skipping draft tokens would be the way back if a workload shows a gain.

**Why host compute.** A host-tier hit costs a 2.7 MB copy over PCIe (about
110 us) that the GPU waits for; the CPU computes the same expert for one token
in about 70 us on 8 cores while the GPU runs the resident experts, and moves a
10 KB row. On novel prompts warm decode drops about 7%; recurring experts still
reach VRAM through admission.

**Why.** On recorded decode traces the cache hit rate dominates decode time, and
on-disk layout barely matters because drives split large reads into small
commands anyway. An explicit pinned host tier makes memory use and copy timing
deterministic, where the page cache competes with everything else on the machine.

## Concurrent requests

`oominf serve --max-streams N` (default 2; 1 serves one request at a time, to
completion) serves up to N requests at once with continuous batching. Requests
beyond those wait in a bounded queue (`--max-queued`, default 16); a request
arriving when the queue is full is refused at once with 429 and `Retry-After: 5`
on both APIs, so sustained overload cannot grow memory (`/v1/stats` reports
`queued` and `rejected`).

- **Batched step** (`Model::step_many`, `QwenModel::step_many`). One step
  carries tokens of several sequences, each with its own KV caches, GDN
  recurrent and conv state, PLE state, draft-head state and position. All
  rows live in one buffer; everything that works row by row runs once over
  all of them (embedding, hyper-connections, the GDN and attention input and
  output projections, the MoE and the head), and only what carries
  per-sequence state runs per sequence on its own rows (PLE, the GDN conv and
  recurrence, the attention core). Each layer therefore routes all rows
  together and fetches the union of their experts once. Each sequence can
  rewind its own rejected drafts afterwards. Sessions are trait objects; the
  model reaches its own sessions' state through `Session::as_any_mut`.
- **Scheduler** (`oominf-server/src/engine/scheduler.rs`). Requests join and
  leave between steps. Each loop runs one prefill slice (the oldest admitted
  request still prefilling) and then one decode step for every decoding
  request. A prompt prefills whole, or in `--prefill-slice` pieces when other
  requests are decoding. Stop tokens, token limits, sampling, events and
  cancellation stay per request; a finished sequence goes to the prefix cache,
  clearing (and so saving to a prefix store) the one it displaces.
- **Admission.** A request is admitted while its sequence, grown to its token
  limit, fits in device memory beside the active sequences grown to theirs
  (`Model::sequence_bytes` against free memory plus what the expert tier can
  give back, less the step headroom); otherwise it queues. A new session takes
  its state's memory from the expert tier (`release_vram`) rather than from the
  step headroom, and batched steps keep that headroom free.
- **Token budget** (`oominf-server/src/budget.rs`). Each step carries every
  decoding stream's next token (worth 1) and the drafts (prompt lookup when it
  matches, else the draft head's) whose expected value pays for the width they
  add: draft j of a stream is worth a^j, a being that stream's recent
  acceptance. The allocator takes candidates by value and picks the width that
  maximises expected tokens per millisecond under a step cost curve
  (`--step-cost`, default measured below, refined by every measured step) plus
  the draft head's cost per drafted token, within `--max-step-tokens` (default
  16, the decode GEMV's limit; wider steps run prefill GEMMs). With one
  decoding request the step is exactly the one-request engine's (its
  configured drafts).

**Why one batched step.** Decode is bound by routed-expert misses, and a step
fetches the union of its rows' experts once per layer, so rows of different
sequences share the misses their experts have in common and every dense weight
is read once. Measured with `oominf bench --streams` (max-perf config, warm
tiers, 16 different requests, one token per sequence per step): 1 sequence
22 ms per step (45 tok/s), 2 sequences 37 ms (54 tok/s), 4 sequences 52-55 ms
(73-77 tok/s), 8 sequences 115-120 ms (67-70 tok/s), 16 sequences 254-257 ms
(62-63 tok/s). The plateau comes early because unrelated requests share few
experts: 8 sequences route about as many records per step as 8 verified drafts
of one sequence (2,266 against 2,317), but across steps their working set
outgrows the VRAM tier (hit rate 68% against 84%), so host-tier copies
dominate. Running each sequence's token mixer on its own first cost about
10 ms per extra sequence (its dense GEMVs and about 25 kernel launches per
layer); batching every row-wise operation removed that (8 sequences 146 to
120 ms per step).

Measured through the server (`bench/http_bench.py --concurrency`, K
concurrent streaming requests of 128 tokens with different short prompts,
temperature 0, the same max-perf config; median of two rounds):

| K | `--max-streams 1`: aggregate tok/s | TTFT p50 / max (s) | `--max-streams 8`: aggregate tok/s | per-stream tok/s | TTFT p50 / max (s) | ITL p50 / p95 / max (ms) |
|---|---|---|---|---|---|---|
| 1 | 43.4 | 0.55 / 0.55 | 43.6 | 53.4 | 0.55 / 0.55 | 25 / 41 / 58 |
| 2 | 42.6 | 0.64 / 3.6 | 48.0 | 31.3 | 0.64 / 1.3 | 40 / 56 / 675 |
| 4 | 41.5 | 6.7 / 9.9 | 54.9 | 16.9 | 2.1 / 2.8 | 51 / 68 / 739 |
| 8 | 39.9 | 13.4 / 23.1 | 47.3 | 7.1 | 3.7 / 5.9 | 126 / 210 / 812 |

With `--max-streams 4` the same runs gave 47.3, 47.8 and 47.4 tok/s at K = 2,
4 and 8 (K = 8 queues four requests: TTFT p50 9.5 s); the two K = 4 runs,
which schedule identically, differ by 15% (54.9 against 47.8), the run to run
spread of these numbers. One request decodes exactly as before (MTP drafts, 53 tok/s per stream). With
several, aggregate throughput rises 1.1-1.3x and the queueing delay before a
request's first token falls 3-4x, at the cost of each stream's speed. The
aggregate gain is smaller than the steady state above because each new
request's prefill (about 0.6 s for these prompts, a sweep of most layers'
experts) stalls the others' decoding (the 0.7-0.8 s maximum gaps), and one
stream alone already gains about 1.2x from its drafts, which batched steps
mostly drop (the budget finds them not worth the width beside other streams'
next tokens).

**Why prefills are not sliced by default.** A prefill of any size above a few
hundred tokens touches most of every layer's experts, so its cost is about one
sweep of the expert tiers whatever its length. Slicing a 2k-token prompt
between other streams' steps (4 concurrent requests, one of them long) cut the
others' longest stall from 3.6 s to 2.2 s (512-token slices) or 2.7 s (1024)
but raised the long request's time to first token from 3.5 s to 9.8 s or
6.9 s and lowered aggregate throughput from 38 to 32-33 tok/s.
`--prefill-slice` is there for workloads that prefer the shorter stall. A stall
shorter than a sweep needs the decoding streams' rows to ride along inside the
layer-major prefill, which fetches every layer's experts anyway.


## Correctness

Every change is checked against the model's reference implementation
(`TESTING.md`). Protocols with concurrency (tier staging, slot reuse and
retirement, CUDA graph replay through a slot table) are specified in TLA+
(`specs/`) and model-checked, including deliberately broken variants that the
invariants must catch.

**Why.** Numerical drift and use-after-overwrite bugs both produce output that
looks plausible. Fixed references and checked protocols make them fail loudly.

## Adding a model

A new model family touches four places, nothing else:

1. **A model crate** (`crates/oominf-models-<family>`): the model written against
   `B: Backend`, implementing `Model`/`Session` and exposing
   `open(backend, files, options, experts)` and its `MODEL_TYPE`. Operations the
   backend lacks are added to the matching `oominf-core` trait (described by
   what the model needs) and implemented in each backend.
2. **A converter adapter** (`oominf-convert/src/adapters/`): which release
   tensors are dense, gathered tables or expert-record parts, plus one arm in
   `adapters::for_config`.
3. **An output parser** (`oominf-server/src/parse.rs`): the family's reasoning
   and tool-call format behind `OutputParser`, plus one arm in `parser_for`.
4. **The registry** (`oominf-models/src/lib.rs`): one arm in `open` and an entry
   in `SUPPORTED`.

Reference fixtures for the new family follow `reference/` and the gates in
`TESTING.md`.

## Adding a platform

A new platform is one crate (e.g. `oominf-rocm`, `oominf-metal`) that implements
every `oominf-core` operation trait for its device type, so it is a `Backend`:

1. `Memory` and `Transfer`: device buffers, uploads and downloads, a copy queue,
   events, and pinning host memory (a no-op where the platform does not need it).
2. The compute traits, with kernels in that platform's language (HIP, Metal,
   Ascend C or Triton). Each kernel must pass the same fixtures and gates as the
   CUDA one; speed differences are fine, output differences beyond the budgets
   are not.
3. One line in the CLI (`oominf/src/load.rs`, `open_model`) to construct it.

Models, tiers, the server and the tests are reused unchanged.
