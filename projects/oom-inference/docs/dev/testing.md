# Testing

Correctness is defined against the model's reference implementation, never
against another engine. Speed changes must not move results outside the gates
below.

| Layer                          | What it proves                                                                                                                                                                                                                                                                                                                                                                              | Command                                                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Unit tests (CPU)               | format round trips and checksums, converter byte copies, cache policies, PLE hashing, server parsing, sampling and API shapes, the batched engine's per-request stops, limits, cancellation and caching, the token budget, memory planning (cgroup limits, shrinking, the minimum), every expert read path, the bounded queue's 429s, and the tier fault-injection tests below              | `cargo test --workspace` (builds without the CUDA toolkit: without `nvcc` the kernels are skipped with a warning) |
| Tier fault injection (CPU)     | on a mock device whose copies run as late as allowed, with reads delayed or failed by a hook: records handed out are intact on every read path; slow lookahead cannot exhaust a minimum host tier; slow stage-ahead waits for a one-slot staging ring; after a failed read no slot is reused while reads are in flight or copies pending; pinning retries smaller; VRAM allocation degrades | `cargo test --release -p oominf-tiers --lib tiered`                                                               |
| Stage fixtures                 | every stage of one decoder layer matches the reference                                                                                                                                                                                                                                                                                                                                      | `oominf check-layer`                                                                                              |
| Whole-model chain              | all layers, final mixer and logits match the reference end to end                                                                                                                                                                                                                                                                                                                           | `oominf check-model`                                                                                              |
| Protocol specs                 | expert tiering never exposes a partially staged or reused slot                                                                                                                                                                                                                                                                                                                              | `specs/run.sh ci`, `specs/run.sh bugs`                                                                            |
| Speculative decoding           | greedy decoding with MTP drafts produces the same tokens as one-token steps, except where the one-token run's top two logits are within 0.05 (near ties, reported)                                                                                                                                                                                                                          | `OOMINF_MODEL=<model.oom> cargo test --release -p oominf-models-qwen --test speculative -- --ignored`             |
| Expert kernel                  | the tensor-core NVFP4 kernel matches an f64 reference on random records at both activation precisions (fp32 exact, bf16-rounded), and its throughput                                                                                                                                                                                                                                        | `cargo test --release -p oominf-cuda --test moe_tiled -- --ignored --nocapture`                                   |
| Decode GEMV                    | `gemm_bf16` (up to 4 rows) and FP8 `gemm_fp8` (GEMV and the dequantize-then-cuBLAS path) match f64 references; throughput on the model's dense shapes past L2                                                                                                                                                                                                                               | `cargo test --release -p oominf-cuda --test gemv -- --ignored --nocapture`                                        |
| KV cache formats and attention | fp32 rows round-trip exactly; compressed rows round-trip with Lloyd-Max distortion; attention over a compressed cache tracks fp32; prefill-sized attention under sparse masks matches an f64 reference; a cache in host memory gives bit-identical attention                                                                                                                                | `cargo test --release -p oominf-cuda --test kv_cache -- --ignored --nocapture`                                    |
| Sequence snapshots             | a saved and restored sequence continues with bit-identical logits and drafts                                                                                                                                                                                                                                                                                                                | `OOMINF_MODEL=<model.oom> cargo test --release -p oominf-models-qwen --test snapshot -- --ignored`                |
| Batched steps                  | a step over three sequences of different lengths (one prefilled layer by layer) with different widths, sequences sitting steps out and rewinding their own drafts, matches twins stepped alone: to the bit until the first step wider than 4 tokens (1.2e-6 in it; gate 1e-4, same argmax), then as speculative decoding does (argmax except near ties, gate 1e-1)                          | `OOMINF_MODEL=<model.oom> cargo test --release -p oominf-models-qwen --test multistream -- --ignored`             |
| Prefix checkpoints             | a sequence rewound to a prefill checkpoint and fed a new continuation matches prefix-then-continuation bit for bit, also after save and load                                                                                                                                                                                                                                                | `OOMINF_MODEL=<model.oom> cargo test --release -p oominf-models-qwen --test checkpoint -- --ignored`              |
| GPU smoke tests                | a real model loads, serves and completes                                                                                                                                                                                                                                                                                                                                                    | `OOMINF_MODEL=<model.oom> cargo test --workspace -- --ignored`                                                    |

## Tier fault injection

`crates/oominf-tiers/src/tiered_tests.rs` runs `TieredExperts` on a CPU stand-in
for the device (`src/mock.rs`) over a small synthetic model written to a temporary
directory. The stand-in queues copies and runs them only when an event covering
them is waited on, so a tier that reuses a copy's source or destination too early
copies the wrong bytes. A `FaultHook` on the reader delays reads (the read writes
its buffer only after the delay) or fails them, and every record a fetch hands
out (in VRAM or, for host compute, in host memory) is compared byte for byte.

The tests found four defects, each fixed with the test that exposed it: a fetch
failed when lookahead reads pinned a small host tier's slots; a staging-ring
wrap inside an open stage-ahead closed the batch and lost reads not yet
submitted; waiting for a copy of the still-open fetch returned at once; an error
part-way through placing a fetch left VRAM slots marked resident. The last three
need a staging ring smaller than one layer, which only became possible with the
memory planning here.

## Reference fixtures

`reference/make_fixtures.py` runs Hugging Face's own model code on CPU with
the release weights (see [`reference/README.md`](../../reference/README.md)): a fixed chat-templated prompt
as prefill, then three teacher-forced decode steps, in fp32 (the truth) and
bf16 (the error budget). It writes:

- per-layer stage fixtures (`layer-NNN/`): every stage boundary, caches and
  recurrent state;
- a whole-model chain (`model/`): every layer's output, the final mixer output,
  full logits and top-20 log-probabilities;
- a long-context layer-3 fixture (`layer-003-long/`, `--long`): a 2734-token
  prompt, long enough that the QSA indexer keeps only its top 512 blocks, so the
  sparse selection is exercised (683 of the prefill queries drop blocks).

Regenerating is deterministic: identical inputs give byte-identical files.

## Gates

**Budget.** For each stage, the reference's own bf16-vs-fp32 error. A bf16
implementation that is as faithful as the reference's own bf16 run sits at a
ratio of 1.0.

**`check-layer`** runs one layer twice:

- _isolated_: every stage is fed the reference's exact inputs, so each error
  belongs to that stage alone. Every stage must stay within 1.5x of the worst
  budget for that stage across steps. Integer stages (top-k expert sets, n-gram
  ids, indexer masks) are compared as sets and must match. The indexer mask is
  computed from the reference's exact block scores here, so it tests the
  selection rule itself.
- _chained_: the layer runs from its input with state carried across steps;
  reported, not gated, because near-tie routing flips are legitimate.

**`check-model`** runs every layer:

- _layer-isolated_: each layer is fed the reference's input for that layer;
- _chained_ (gated): logits must be at least as close to fp32 as the
  reference's own bf16 run (rms error within 1.05x and no fewer top-1 matches)
  on every step.

Caching and scheduling only move bytes, so tier or policy changes should leave
`check-model` output byte-identical with `--host-compute 0` (with host compute,
which experts run on the CPU depends on timing and they round differently). No
test enforces this; compare the output by hand. The tier protocol itself is
covered by the specs and by the fault-injection tests below.

**Batched steps** (`--max-streams` above 1) change only which rows share a
GEMM; the `multistream` test above checks each sequence against itself run
alone. Why its tolerance has two levels: up to 4 tokens a step's dense GEMVs
reduce every row the same way whatever the row count, so results are bit for
bit; wider steps run wider GEMV instances that split the reduction differently,
which changes rounding in the same way a draft verification step does against
one-token steps, and the compressed KV cache and top-k routing carry that
rounding forward.

## Protocol specs

`specs/ExpertTiering.tla` models the disk, host and VRAM tiers, asynchronous
staging, kernels running behind the host, and CUDA graphs that bake either
slot addresses or a slot table's address. TLC checks the invariants in
[`specs/README.md`](../../specs/README.md); each deliberate `Bug` variant must be caught by the
invariant it targets (`run.sh bugs`).

## KV cache format and the gates

The default KV cache is lossy (`k8v6`). `check-model` runs the default; its
chained logits gate holds, while individual layers sit above the per-layer bf16
budget. The exact path is checked with `--kv-cache fp32` (byte-identical to the
engine before compression existed) and stays the reference for numerical changes.

## Running the GPU gates

GPU commands (the gates, `#[ignore]`d GPU tests, `oominf bench`, `serve`,
`tune`, `doctor`) assume exclusive use of the device; on a shared machine hold
its lock around them (`flock <lock file> <command>`, or `bench/gate.sh --lock
<lock file>`). With the model at `$M` and fixtures at `$F`:

    oominf check-layer --model $M --fixtures $F/layer-000 --layer 0
    oominf check-layer --model $M --fixtures $F/layer-001 --layer 1
    oominf check-layer --model $M --fixtures $F/layer-003 --layer 3
    oominf check-layer --model $M --fixtures $F/layer-003-long --layer 3
    oominf check-model --model $M --fixtures $F/model
    oominf check-model --model $M --fixtures $F/model --kv-cache fp32

Each exits non-zero when a gate fails.

`bench/gate.sh --model $M --fixtures $F [--lock <lock file>]` runs all of the
above plus the CPU tests, the kernel tests and the GPU integration tests
(speculative, mtp_lookup, snapshot, checkpoint, multistream) and ends with
`ALL GATES PASSED` or the list of failures.

## Outcome scoring (lossy modes)

`oominf score` teacher-forces the last `--tail` tokens of a long document
(plain-text tokenization) and writes each position's logits (`--out`) or compares
them with a reference run (`--against`): KL divergence, top-1 agreement and
perplexity. The reference file binds token-id and checkpoint checksums.

At long context this model is very sensitive to rounding: fp32 against fp32 with
only the prefill chunk size changed gives a mean KL of about 0.03 and about 90%
top-1 agreement on a 32k-token document, because discrete expert routing
amplifies rounding-level differences through the layers. Judge a lossy mode
against that band (several equivalent fp32 runs), not against a single reference.
