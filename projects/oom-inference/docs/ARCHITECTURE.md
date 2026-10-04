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
| `oominf-tiers` | Expert sources: `TieredExperts` (VRAM slots, pinned host arena filled by direct I/O, cache policies) and `DiskExperts` | core, format |
| `oominf-models-qwen` | Qwen 3.8 Flash, generic over `B: Backend` | core, format |
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
  activations unquantised (W4A16), fp32 accumulation.
- Dense weights: bf16 as released. Decode GEMVs read fp32 activations directly;
  prefill GEMMs round activations to bf16 for tensor cores.
- Residual stream, norms, softmax, recurrent state and KV cache: fp32.
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
- Prefill streams through a one-layer stage instead of evicting decode-hot
  experts, and runs layer by layer so each layer's experts load once per prompt.
- During decode (and draft verification), layer L+1's router applied to layer
  L's input predicts the next experts; predicted disk misses are read into the
  host tier. Routing, not prediction, decides which experts run.
- Groups with different record layouts (the decoder layers' NVFP4 experts, the
  MTP layer's bf16 experts) get separate tiers behind one source. A small group
  gets one VRAM chunk and has its records read into the host tier at start-up:
  measured on the MTP experts, more device slots barely shorten drafting while
  every slot taken from the main tiers costs decode hits.

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
