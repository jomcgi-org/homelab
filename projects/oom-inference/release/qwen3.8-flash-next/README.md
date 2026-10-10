---
library_name: oominf
pipeline_tag: text-generation
inference: false
license: other
license_name: qwen-community-1.0
license_link: LICENSE
base_model: Qwen/Qwen3.8-Flash-Next
base_model_relation: quantized
tags:
  - oominf
  - qwen3.8
  - moe
  - nvfp4
  - offload
  - rtx-4090
model-index:
  - name: Qwen3.8-Flash-Next-NVFP4-oominf
    results:
      - task:
          type: text-generation
          name: Text Generation
        dataset:
          name: GSM8K
          type: gsm8k
        metrics:
          - type: accuracy
            value: 97.73
            name: Accuracy (full 1,319, t0.6, top-p 0.95, max 8,192, oomeval)
        source:
          url: https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/dev/measurements.md
          name: oomeval
      - task:
          type: text-generation
          name: Text Generation
        dataset:
          name: MMLU-Pro (200 of 12,032)
          type: mmlu_pro
        metrics:
          - type: accuracy
            value: 77.0
            name: Accuracy (greedy, max 4,096 tokens, oomeval)
        source:
          url: https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/dev/measurements.md
          name: oomeval
      - task:
          type: text-generation
          name: Text Generation
        dataset:
          name: RULER 95k
          type: ruler
        metrics:
          - type: accuracy
            value: 98.46
            name: Accuracy (52 items, greedy, oomeval)
        source:
          url: https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/dev/measurements.md
          name: oomeval
---

# Qwen3.8-Flash-Next NVFP4 for oom-inference

[Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) (125B total, 6B active)
in the [oom-inference](https://github.com/jomcgi-org/homelab/tree/main/projects/oom-inference)
weight format, for serving on **one 24 GB GPU with 64 GB of RAM**. The engine keeps routed
experts across VRAM, pinned RAM and NVMe, so the model does not need to fit in memory.

This is
[`RadixArk/Qwen3.8-Flash-Next-NVFP4`](https://huggingface.co/RadixArk/Qwen3.8-Flash-Next-NVFP4)
with its bytes laid out one record per expert. No weight is changed: the NVFP4 routed experts,
BF16 dense weights and FP8 n-gram tables are exactly as released. It is only for `oominf`; it
is not a Transformers, Safetensors or GGUF checkpoint.

How it works: [How to serve 134 GB of model weights with 24 GB VRAM / 64 GB RAM](https://jomcgi.dev/blog/125b-on-a-4090).

## Quality

Measured through `oominf serve`'s OpenAI-compatible API with
[oomeval](https://github.com/jomcgi-org/homelab/tree/main/projects/oom-inference/evals), on the
files in this repository with `serve` defaults (fp32 compute, k8v6 KV cache).

### Against the published scores, same protocol

Each row runs the full set with the sampling settings and sample count published for the score it
is compared with (for GPQA-Diamond, where Qwen publish none, Qwen's recommended thinking-mode
sampling).

| Benchmark                  | Protocol                                   | oominf                  | Unquantised BF16 | NVFP4 on SGLang |
| -------------------------- | ------------------------------------------ | ----------------------- | ---------------: | --------------: |
| GSM8K (1,319)              | t0.6, top-p 0.95, max 8,192, 1 sample      | **97.73** (95% CI ±0.80) |   97.12 to 97.50 |           97.27 |
| GPQA-Diamond (198)         | thinking, t1.0, top-p 0.95, top-k 20, max 65,536, 1 sample | 88.1 on the first 118 of 198 (partial, 95% CI ±5.8) |             91.7 |      not reported |
| AIME26 (30 x 8)            | thinking, t1.0, top-p 0.95, max 130,000, pass@1 over 8 | not yet run |              100 |           98.75 |

- GSM8K: 0.3% of answers hit the 8,192-token limit; median decode 37.2 tok/s.
- GPQA-Diamond is partial: the first 118 rows of the dataset file, not a random subset, so it
  is not yet a comparison with the published score. 4 answers hit the 65,536-token limit and
  score as wrong.
- BF16 GSM8K and AIME26 and the SGLang column are RadixArk's, from their
  [model card](https://huggingface.co/RadixArk/Qwen3.8-Flash-Next-NVFP4); their BF16 runs were on
  an earlier revision of the model. BF16 GPQA-Diamond is from the
  [Qwen model card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next), which does not state its
  harness or sample count.
- Prompts and answer extraction differ between harnesses, so differences of a point or two are
  within what the harness alone can move.

### Defaults against max-perf

Paired runs on the same items, temperature 0. **Max-perf** adds
`--dense fp8 --expert-precision bf16`.

| Benchmark (items)        | Defaults | Max-perf |
| ------------------------ | -------: | -------: |
| GSM8K (300 of 1,319)     |    97.33 |    97.00 |
| MMLU-Pro (200 of 12,032) |    77.00 |    77.50 |
| RULER, 8k and 32k (104)  |    99.23 |    99.62 |
| RULER, 95k (52)          |    98.46 |    98.08 |

- No difference is outside its 95% interval. RULER covers single needle, multi-key, multi-value
  and variable tracking.
- MMLU-Pro ran at a 4,096-token limit: 17.5% of answers were cut off in both columns, so its
  absolute score understates the model.
- Numerics are checked against the model's reference implementation on every change; see
  [testing](https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/dev/testing.md).

## Performance

RTX 4090 (24 GB, PCIe 4.0 x16), Ryzen 7 7800X3D (8 cores), 62 GiB RAM, Kingston KC3000 NVMe,
one request at a time.

| Workload                                 | Defaults          | Max-perf          |
| ---------------------------------------- | ----------------- | ----------------- |
| Decode, short and medium prompts         | 33 to 36 tok/s    | 40 to 48 tok/s    |
| Prefill, 21.9k-token prompt, warm        | not measured      | 3,118 tok/s       |
| 95k-token prompt: time to first token    | 36.9 s            | 29.7 s            |
| 95k-token prompt: decode                 | 16.7 tok/s        | 18.0 tok/s        |

- Decode ranges are medians over the GSM8K and MMLU-Pro runs above, and for max-perf also the
  served demo (48.4 tok/s warm). The 21.9k prefill and demo rows also used
  `--attention-precision bf16`. Two streams (the default) share each step: about 48 tok/s in
  total, about 31 each.
- With less RAM the engine shrinks its tiers instead of failing; it has served correctly in an
  8 GiB container at about 13 tok/s.
  [Hardware guide](https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/guide/hardware.md),
  [full measurement log](https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/dev/measurements.md).

## Requirements

- `oominf` built from source:
  [install guide](https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/guide/install.md).
- 64-bit Linux, an NVIDIA GPU with 24 GB, CUDA 13.
- About 135 GB on a local NVMe drive. The engine reads experts from it while serving, so a slow
  drive slows decode.

## Download and serve

```bash
hf download jomcgi-org/Qwen3.8-Flash-Next-NVFP4-oominf \
  --revision oominf-format-0 \
  --local-dir ~/models/qwen3.8-flash-next.oom

oominf verify ~/models/qwen3.8-flash-next.oom    # every checksum; "0 mismatches"
oominf serve --model ~/models/qwen3.8-flash-next.oom
```

`serve` listens on `127.0.0.1:8091` with OpenAI and Anthropic APIs. See the
[quickstart](https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/guide/quickstart.md)
and [configuration](https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/guide/configuration.md).

## Files

| File              | Size      | Contents                                                   |
| ----------------- | --------- | ---------------------------------------------------------- |
| `experts.bin`     | 73.1 GB   | 49 layers of routed experts, one 4 KiB-aligned record each |
| `tables.bin`      | 51.2 GB   | FP8 PLE n-gram embedding tables                            |
| `dense.bin`       | 10.1 GB   | everything else                                            |
| `index.json`      | 1.1 MB    | tensors, records and an xxh3 checksum for each             |
| tokenizer, config and chat template | | copied from the release                          |

[Format specification](https://github.com/jomcgi-org/homelab/blob/main/projects/oom-inference/docs/dev/format.md).
Revisions are tagged by format version (`oominf-format-0`); an engine refuses a format version
it does not know.

## Provenance

| Field              | Value                                                                 |
| ------------------ | --------------------------------------------------------------------- |
| Base model         | `Qwen/Qwen3.8-Flash-Next`                                             |
| Quantised release  | `RadixArk/Qwen3.8-Flash-Next-NVFP4` at `7b719225242aacd3dbd3f9407468c2ee9a9d2594` |
| Quantisation       | NVIDIA ModelOpt 0.46.0 NVFP4 W4A4, routed experts only (RadixArk)     |
| Converted with     | `oominf convert`, homelab commit `069b1eec3`                             |

## License

[Qwen Community License 1.0](LICENSE), as for the base model. Credit to the Qwen team for the
model and RadixArk for the NVFP4 release.
