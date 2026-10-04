# Reference fixtures

Per-layer reference fixtures for Qwen 3.8 Flash. They define "correct" for oominf: every stage
boundary of a decoder layer, produced by the **model's reference implementation** (HF
transformers `qwen4_exp`), never by another engine.

- CPU only, deterministic: two runs at 8 threads produce byte-identical files.
- Only the tensors a layer needs are read from the release checkpoint
  (`flash-next-nvfp4`, RadixArk Qwen3.8-Flash-Next-NVFP4).
- Fixtures live outside the repo: `/disks/nvme-02/src/oominf-data/fixtures/qwen38-flash/`.

## Regenerate

```sh
cd projects/oom-inference/reference
export UV_CACHE_DIR=/disks/nvme-02/src/.toolchains/uv-cache HF_HUB_OFFLINE=1
uv sync
CUDA_VISIBLE_DEVICES= nice -n 19 taskset -c 8-15 uv run python make_fixtures.py --layers 0 1
```

About 20 s with a warm page cache, peak RSS about 14 GB (the fp32 run densifies all 512 experts of a
layer). Layer N>0 is chained: its input is layer N-1's `layer_out` fixture for the same mode,
dtype and step, so generate layers in order.

## Workload

- Prefill: the checkpoint's own chat template applied to one user message
  (`USER_MESSAGE` in `make_fixtures.py`), 73 tokens including the template's system prompt.
- Decode: 3 single-token steps, teacher-forced from `CONTINUATION` (one layer cannot produce real
  next tokens). This exercises the cached decode paths: GDN conv update and recurrent rule, PLE
  n-gram context and short-conv state.
- No sampling, no randomness. `torch.use_deterministic_algorithms(True)`, `--threads 8` (CPU
  matmul reduction order depends on the thread count; keep it fixed).

## Layout

```
layer-000/
  manifest.json          prompt, token ids, versions, stage descriptions, modes, dtypes
  tolerances.json        per-stage deltas (see below)
  w4a16/{fp32,bf16}/{prefill,decode-1,decode-2,decode-3}.safetensors
  w4a4/{fp32,bf16}/...
layer-001/               same, plus PLE stages
```

Each safetensors file holds the stages of one step (2-D `[tokens, features]` unless noted):

| Stage | Meaning |
|---|---|
| `residual_in` | layer input residual `[T, 4*2560]` (layer 0: token embedding repeated over the 4 hc streams) |
| `ple.ngram_ids`, `ple.ngram_embed`, `ple_out` | PLE hashed row ids `[T, 16]` int64, gathered+dequantised embedding `[T, 2560]`, PLE output added to the residual (layer 1 only) |
| `attn_hc.mixed`, `attn_hc.inject` | attention hyper-connection: block input `[T, 2560]`, injection weights `[T, 4]` |
| `gdn.in_proj_{qkv,z,b,a}`, `gdn.conv_out`, `gdn.core_out`, `gdn.norm_out` | Gated DeltaNet internals: projections, causal conv + SiLU, delta-rule output `[T, 48, 128]`, gated RMSNorm `[T*48, 128]` |
| `mixer_out` | token mixer output `[T, 2560]` |
| `attn_combine_out` | residual after the attention combine |
| `mlp_hc.mixed`, `mlp_hc.inject` | MLP hyper-connection |
| `router_logits`, `topk_ids`, `topk_weights` | `[T, 512]`, `[T, 10]` int64, renormalised weights |
| `routed_out`, `shared_out`, `shared_gate_logit`, `moe_out` | routed expert sum, shared expert (pre-gate), its gate logit, MoE output |
| `layer_out` | layer output residual |
| `state.conv`, `state.recurrent` | GDN conv state `[1, 10240, 4]`, recurrent state `[1, 48, 128, 128]` after the step |
| `state.ple_conv`, `state.ple_tokens` | PLE short-conv state `[1, 10240, 9]`, n-gram token context `[1, 2]` int64 (layer 1) |

Stages are captured with forward hooks on HF's own modules and wrappers around its module-level
functions (`causal_conv1d_*`, `torch_*_gated_delta_rule`); nothing outside HF code computes them.

## Quantisation: what HF does and what the reference does

**HF transformers 5.16.1 cannot load this checkpoint as released.** Its `nvfp4` quantizer
raises "Loading pre-quantized NVFP4 checkpoints is not supported yet" and requires sm100+ CUDA;
there is no `modelopt` quant method at all. It also does not know the converter-declared
`text_config.ple_embedding_dtype` (FP8 PLE). So the reference uses HF's modules with dense
weights produced by the producer's own dequantisation:

**Routed experts, NVFP4 weights** (ModelOpt 0.46.0, the producer named in `hf_quant_config.json`;
`modelopt/torch/quantization/qtensor/nvfp4_tensor.py`, `NVFP4QTensor.quantize` / `.dequantize`):

- `weight` U8 `[out, in/2]`: two E2M1 codes per byte, **low nibble = even element**
  (`packed = (q[..., 1::2] << 4) | q[..., 0::2]`). Code = sign bit 3 | magnitude index into
  `[0, 0.5, 1, 1.5, 2, 3, 4, 6]`.
- `weight_scale` F8_E4M3 `[out, in/16]`: one scale per 16 consecutive inputs, row-major, linear.
- `weight_scale_2` F32 scalar: per-tensor second-level scale.
- `w = e2m1[code] * (float(weight_scale) * weight_scale_2)`, computed in fp32, then cast to
  the run dtype.
- HF's fused `gate_up_proj[e]` is `cat(gate_proj, up_proj)` along the output dim.

**Activations: the checkpoint is W4A4.** Each expert projection carries a static `input_scale`
(gate and up are equal for every expert in layer 0; down differs). ModelOpt exports
`input_scale = amax / (6 * 448)` (`NVFP4QTensor.get_activation_scaling_factor`). The `w4a4` mode
emulates activation quantisation exactly as ModelOpt's runtime fake-quant kernel does
(`modelopt/torch/kernels/quantization/gemm/fp4_kernel_hopper.py` `fp4_fake_quant_block`, with
`fp8_quantize_scale` and `fp4_round_magnitude` from
`modelopt/torch/kernels/quantization/common/nvfp4_quant.py`), per 16-element block:

```
s = fp8_e4m3(min(block_amax / (6 * input_scale), 448)) * input_scale
s = 1.0 if s < 1e-5
y = sign(x) * round_e2m1(|x| / s) * s        # ties to even
```

applied to every routed expert input: the gate and up inputs (each with its own `input_scale`)
and the down input. Hardware W4A4 GEMMs compute the same quantities but may differ in the last
ulp of the block scale. The `w4a4` experts forward is HF's `Qwen4ExpTextExperts.forward` line for
line plus these calls; with quantisation off it reproduces HF bit-exactly (checked on every run).

**Which mode is "as released" is a policy decision for oominf**, not something this directory
settles. Measured effect (fp32, w4a4 vs w4a16, worst over the 4 steps):

| Layer | `routed_out` rms rel | `moe_out` rms rel | `layer_out` rms rel | `layer_out` cosine |
|---|---|---|---|---|
| 0 | 14.5% | 6.5% | 3.6% | 0.99936 |
| 1 (chained) | 13.2% | 12.2% | 4.3% | 0.99910 |

By layer 1 the difference also flips routing: 9 of 73 prefill tokens pick a different expert set.

**PLE table (layer 1).** 128 F8_E4M3 shards `[2,500,012, 160]` concatenate (HF's own
`conversion_mapping.py`, `Concatenate(dim=0, num_shards_attribute="split_ngram_parts")`) into
the padded `[320,001,536, 160]` table. Rows are dequantised `fp8 * weight_scale` (one BF16 scalar
per table). Only the rows a step touches are read (`LazyPleTable`). The checkpoint's
`layer_multipliers`, `ngram_heads_offsets` and `ngram_heads_vocab_sizes` buffers match HF's own
recomputation exactly.

## Tolerance baseline

`tolerances.json` compares runs against the fp32 run of the same mode:

- `{mode}/bf16_vs_fp32`: what bf16 arithmetic alone costs. **Use this as the per-stage tolerance
  scale** for an implementation computing in bf16.
- `w4a4_vs_w4a16/fp32`: the effect of activation quantisation.

Metrics per stage: `max_abs`, `max_abs_over_absmax` (max error over the reference's absmax),
`rms_rel` (RMS error over RMS of the reference), `cosine`. Top-k ids use set comparison:
`set_mismatch_frac` (fraction of reference picks missing) and `tokens_differing`.

Layer 0, w4a16, bf16 vs fp32, worst over prefill and 3 decode steps:

| Stage | max_abs/absmax | rms_rel | min cosine |
|---|---|---|---|
| `attn_hc.mixed` | 7.2e-3 | 2.5e-3 | 0.999997 |
| `gdn.in_proj_qkv` | 4.1e-3 | 2.6e-3 | 0.999997 |
| `gdn.conv_out` | 4.9e-3 | 2.9e-3 | 0.999996 |
| `gdn.core_out` | 5.6e-3 | 3.9e-3 | 0.999995 |
| `gdn.norm_out` | 8.2e-3 | 5.1e-3 | 0.999987 |
| `mixer_out` | 6.4e-3 | 5.0e-3 | 0.999988 |
| `attn_combine_out` | 9.5e-3 | 7.5e-3 | 0.999973 |
| `mlp_hc.mixed` | 1.5e-2 | 9.0e-3 | 0.999959 |
| `router_logits` | 6.3e-3 | 2.1e-3 | 0.999998 |
| `topk_weights` | 8.8e-3 | 7.2e-3 | 0.999975 |
| `shared_out` | 7.7e-3 | 3.8e-3 | 0.999993 |
| `routed_out` | 2.4e-2 | 2.1e-2 | 0.999772 |
| `moe_out` | 1.2e-2 | 1.0e-2 | 0.999946 |
| `layer_out` | 1.3e-2 | 8.3e-3 | 0.999966 |
| `state.conv` | 3.3e-3 | 2.2e-3 | 0.999998 |
| `state.recurrent` | 5.6e-3 | 4.7e-3 | 0.999989 |

`topk_ids`: 10 of 73 prefill tokens differ by one expert (set mismatch 1.4%); decode steps agree.
Layer 1 tolerances are cumulative (its inputs already carry layer 0's drift): `layer_out` rms rel
9.7e-3, PLE `ple.ngram_embed` 1.6e-3 and `ple_out` 7.5e-3.

### How to compare an implementation

- **Expect routing flips at near-ties** even between two correct implementations: compare
  `topk_ids` as sets and only fail a flip whose router-logit gap exceeds the logits tolerance.
- **Test stages in isolation** where possible: feed a stage the fixture's input for that stage
  (e.g. `mlp_hc.mixed` plus the fixture `topk_ids`/`topk_weights` into the routed experts) and
  compare its output, so one upstream flip does not cascade.
- An implementation computing in bf16 should land within a small multiple of the
  `bf16_vs_fp32` row for each stage; anything far outside it is a bug, not rounding.

## Limits

- Layers 0 (GDN) and 1 (GDN + PLE) only. Layer 3 is the first sparse full-attention layer; it
  chains through layer 2 and needs the indexer path hooked.
- The fp32 run upcasts BF16 checkpoint values exactly; it is a higher-precision reference, not a
  different model.
