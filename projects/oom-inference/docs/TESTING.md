# Testing

Correctness is defined against the model's reference implementation, never
against another engine. Speed changes must not move results outside the gates
below.

| Layer | What it proves | Command |
|---|---|---|
| Unit tests (CPU) | format round trips and checksums, converter byte copies, cache policies, PLE hashing, server parsing, sampling and API shapes | `cargo test --workspace` |
| Stage fixtures | every stage of one decoder layer matches the reference | `oominf check-layer` |
| Whole-model chain | all layers, final mixer and logits match the reference end to end | `oominf check-model` |
| Protocol specs | expert tiering never exposes a partially staged or reused slot | `specs/run.sh ci`, `specs/run.sh bugs` |
| Speculative decoding | greedy decoding with MTP drafts produces the same tokens as one-token steps | `OOMINF_MODEL=<model.oom> cargo test --release -p oominf-models-qwen --test speculative -- --ignored` |
| Expert kernel | the tensor-core NVFP4 kernel matches an f64 reference on random records, and its throughput | `cargo test --release -p oominf-cuda --test moe_tiled -- --ignored --nocapture` |
| GPU smoke tests | a real model loads, serves and completes | `OOMINF_MODEL=<model.oom> cargo test --workspace -- --ignored` |

## Reference fixtures

`reference/make_fixtures.py` runs Hugging Face's own model code on CPU with
the release weights (see `reference/README.md`): a fixed chat-templated prompt
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

- *isolated*: every stage is fed the reference's exact inputs, so each error
  belongs to that stage alone. Every stage must stay within 1.5x of the worst
  budget for that stage across steps. Integer stages (top-k expert sets, n-gram
  ids, indexer masks) are compared as sets and must match. The indexer mask is
  computed from the reference's exact block scores here, so it tests the
  selection rule itself.
- *chained*: the layer runs from its input with state carried across steps;
  reported, not gated, because near-tie routing flips are legitimate.

**`check-model`** runs every layer:

- *layer-isolated*: each layer is fed the reference's input for that layer;
- *chained* (gated): logits must be at least as close to fp32 as the
  reference's own bf16 run (rms error within 1.05x and no fewer top-1 matches)
  on every step.

Caching and scheduling only move bytes, so tier or policy changes must leave
`check-model` output byte-identical.

## Protocol specs

`specs/ExpertTiering.tla` models the disk, host and VRAM tiers, asynchronous
staging, kernels running behind the host, and CUDA graphs that bake either
slot addresses or a slot table's address. TLC checks the invariants in
`specs/README.md`; each deliberate `Bug` variant must be caught by the
invariant it targets (`run.sh bugs`).

## Running the GPU gates

GPU commands assume exclusive use of the device. With the model at `$M` and
fixtures at `$F`:

    oominf check-layer --model $M --fixtures $F/layer-000 --layer 0
    oominf check-layer --model $M --fixtures $F/layer-001 --layer 1
    oominf check-layer --model $M --fixtures $F/layer-003 --layer 3
    oominf check-layer --model $M --fixtures $F/layer-003-long --layer 3
    oominf check-model --model $M --fixtures $F/model

Each exits non-zero when a gate fails.
