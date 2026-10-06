# Architecture

oom-inference serves Mixture-of-Experts models that are larger than GPU memory
plus host memory, on one consumer machine, for one or two interactive streams.
Routed experts live in three tiers (VRAM, pinned host memory, NVMe) and move on
demand; everything else stays resident on the device.

## Crates

| Crate | Role | Depends on |
|---|---|---|
| `oominf-core` | Platform- and model-neutral interfaces: `Backend`, `Model`/`Session`, `ExpertSource`, `Workspace`, `Probe` | nothing in the workspace |
| `oominf-format` | On-disk weight format: reader, writer, checksums (`FORMAT.md`) | |
| `oominf-convert` | Release checkpoint (safetensors) to the format, one adapter per model family | format |
| `oominf-cuda` | CUDA implementation of `Backend`: device memory, streams, events, cuBLAS, kernels in `kernels/*.cu` | core |
| `oominf-cpu` | Routed experts computed on the host from records in host memory (exact NVFP4 and bf16 decoding, fp32, AVX-512 with a scalar path), on a worker pool | core |
| `oominf-tiers` | Expert sources: `TieredExperts` (VRAM slots, pinned host arena filled by direct I/O, cache policies) and `DiskExperts` | core, format |
| `oominf-models-qwen` | Qwen 3.8 Flash, generic over `B: Backend` | core, cpu, format |
| `oominf-models` | Registry: opens a converted model with the implementation for its `model_type` | core, format, models-* |
| `oominf-server` | OpenAI and Anthropic HTTP APIs, chat templates, output parsers, sampling, prefix reuse | core |
| `oominf` | CLI and composition root: picks the backend, the model (via the registry) and the expert source | all |

Only `oominf-cuda` knows about CUDA. Only `oominf-models-*`, the converter's
adapter and the server's output parser know about a model family. The server
runs any `Model`; the CLI is the one place that names the CUDA backend.

**Why.** New hardware and new models are the two expected kinds of growth. A
platform port implements `Backend` and reuses every model, the tiers and the
server; a new model family reuses the backend, the tiers and the server. Neither
touches the other.

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
  the model rejects. The server, `generate` and `bench` use only these.
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
deterministic, and speculative decoding again equals one-token decoding.
- MTP experts (the draft head): bf16 as released, fp32 activations and
  accumulation.

**Why.** The engine exists to run a frontier model on modest hardware without
making it worse. Rounding that buys nothing is pure loss, and silent precision
trades are how engines drift from the model they claim to run.

## Experts and tiers

- A converted model stores one contiguous record per (layer, expert)
  (`FORMAT.md`), so a miss is one read and one copy.
- **VRAM tier:** record-sized slots in chunked arenas, so whole chunks can be
  given back when a growing KV cache needs memory and taken back for the next
  sequence. **Host tier:** a pinned arena filled with direct (O_DIRECT, io_uring)
  reads, never the page cache. **Disk:** the model files.
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
- During decode (and draft verification), layer L+1's router applied to layer
  L's input predicts the next experts; predicted disk misses are read into the
  host tier. Routing, not prediction, decides which experts run.
- Groups with different record layouts (the decoder layers' NVFP4 experts, the
  MTP layer's bf16 experts) get separate tiers behind one source. A small group
  gets one VRAM chunk and has its records read into the host tier at start-up:
  measured on the MTP experts, more device slots barely shorten drafting while
  every slot taken from the main tiers costs decode hits.

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
draft only when it is the argmax, so output equals one-token decoding; sampling
keeps it with the model's probability of it and otherwise samples the model
without it, so output keeps the model's distribution. Rejected drafts are
rewound: GDN and PLE layers restore the step's starting state and replay their
recurrences over the kept rows; attention layers cut their KV length.

**Why.** Dense weights are read once per step whatever its width, so verifying a
draft costs much less than a second step. The gain is bounded by the draft
acceptance rate (55 to 75% measured) and by the extra routed experts the
drafted token pulls in.

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

**Why host compute.** A host-tier hit costs a 2.7 MB copy over PCIe (about
110 us) that the GPU waits for; the CPU computes the same expert for one token
in about 70 us on 8 cores while the GPU runs the resident experts, and moves a
10 KB row. On novel prompts warm decode drops about 7%; recurring experts still
reach VRAM through admission.

**Why.** On recorded decode traces the cache hit rate dominates decode time, and
on-disk layout barely matters because drives split large reads into small
commands anyway. An explicit pinned host tier makes memory use and copy timing
deterministic, where the page cache competes with everything else on the machine.

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
