# Full forward pass: Qwen 3.8 Flash end to end (2026-10-04)

`oominf check-model` runs all 48 layers (36 Gated DeltaNet, 12 full attention
with the QSA indexer, PLE at layer 1, NVFP4 MoE everywhere) plus the final
hyper-connection mixer and `lm_head` on the RTX 4090, against the reference
whole-model chain (HF `qwen4_exp`, fp32, `reference/make_fixtures.py
--model-chain`): a 73-token chat prompt plus three teacher-forced decode steps.

**Gate: chained logits at least as close to the fp32 reference as the
reference's own bf16 run.** Passed on every step.

| Step | Logits rms_rel (ours / HF bf16) | Top-1 vs fp32 (ours / HF bf16) |
|---|---|---|
| prefill (73 tokens) | 0.049 / 0.071 | 72/73 / 69/73 |
| decode-1 | 0.201 / 0.238 | 1/1 / 1/1 |
| decode-2 | 0.108 / 0.115 | 1/1 / 1/1 |
| decode-3 | 0.070 / 0.083 | 1/1 / 1/1 |

- **Layer-isolated** (each layer fed the reference's fp32 input): every layer's
  own error is at most 0.30x of the reference's bf16 drift at that depth;
  logits match top-1 on all 73 prefill positions with |logprob error| at most
  0.029 on the fp32 top-20 (HF bf16: 2.62).
- **Chained**: error compounds through 48 layers as it does for any bf16-GEMM
  implementation; a few mid-depth decode layers sit at 1.1x to 1.2x of HF
  bf16's compounded drift while the final logits stay at 0.69x to 0.93x.

Tokenizer and chat template reproduce the reference prompt byte for byte
(`oominf tokenize --manifest`). Greedy decode via `oominf generate` produces
coherent text; current speed (1.96 tok/s) reflects reference kernels and no
expert cache, which is the next phase.

## Not yet covered

- The QSA indexer's top-k pruning only engages beyond 512 blocks (about 2048
  tokens); fixtures are 76 tokens, so it needs a long-context fixture.
- W4A4 and the GLM 5.3 Flash model are out of scope here.

## Run

    oominf check-model \
      --model /disks/nvme-02/src/oominf-data/models/qwen38-flash.oom \
      --fixtures /disks/nvme-02/src/oominf-data/fixtures/qwen38-flash/model

## Full output

```
model loaded in 8.6s

== layer-isolated (each layer fed the reference input) ==
-- prefill (T=73, 14.7s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   mixer_out      rms_rel 2.549e-4 cos 1.000000  (HF bf16 9.338e-2, ratio 0.00)
   logits         rms_rel 1.371e-3 cos 0.999999  (HF bf16 7.143e-2, ratio 0.02)
   top-1 vs fp32: ours 73/73, HF bf16 69/73; |dlogprob| on fp32 top-20: ours max 0.029 mean 0.0036, HF bf16 max 2.620 mean 0.2586
   worst layer_out vs HF bf16 budget: layer_out.0 (rms_rel 2.446e-3, ratio 0.30)
-- decode-1 (T=1, 0.3s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   mixer_out      rms_rel 9.075e-5 cos 1.000000  (HF bf16 2.595e-1, ratio 0.00)
   logits         rms_rel 1.255e-3 cos 0.999999  (HF bf16 2.381e-1, ratio 0.01)
   top-1 vs fp32: ours 1/1, HF bf16 1/1; |dlogprob| on fp32 top-20: ours max 0.012 mean 0.0057, HF bf16 max 3.372 mean 0.8829
   worst layer_out vs HF bf16 budget: layer_out.0 (rms_rel 1.129e-3, ratio 0.23)
-- decode-2 (T=1, 0.3s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   mixer_out      rms_rel 3.366e-4 cos 1.000000  (HF bf16 1.188e-1, ratio 0.00)
   logits         rms_rel 1.774e-3 cos 0.999999  (HF bf16 1.153e-1, ratio 0.02)
   top-1 vs fp32: ours 1/1, HF bf16 1/1; |dlogprob| on fp32 top-20: ours max 0.019 mean 0.0063, HF bf16 max 1.292 mean 0.3826
   worst layer_out vs HF bf16 budget: layer_out.0 (rms_rel 1.269e-3, ratio 0.24)
-- decode-3 (T=1, 0.3s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   mixer_out      rms_rel 1.933e-4 cos 1.000000  (HF bf16 8.182e-2, ratio 0.00)
   logits         rms_rel 2.047e-3 cos 0.999998  (HF bf16 8.296e-2, ratio 0.02)
   top-1 vs fp32: ours 1/1, HF bf16 1/1; |dlogprob| on fp32 top-20: ours max 0.012 mean 0.0056, HF bf16 max 0.430 mean 0.1997
   worst layer_out vs HF bf16 budget: layer_out.0 (rms_rel 1.532e-3, ratio 0.28)

== chained (end to end) ==
-- prefill (T=73, 14.4s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   layer_out.47   rms_rel 3.413e-2 cos 0.999418  (HF bf16 4.994e-2, ratio 0.68)
   mixer_out      rms_rel 6.511e-2 cos 0.997882  (HF bf16 9.338e-2, ratio 0.70)
   logits         rms_rel 4.948e-2 cos 0.998775  (HF bf16 7.143e-2, ratio 0.69)
   top-1 vs fp32: ours 72/73, HF bf16 69/73; |dlogprob| on fp32 top-20: ours max 3.325 mean 0.1681, HF bf16 max 2.620 mean 0.2586
   worst layer_out vs HF bf16 budget: layer_out.47 (rms_rel 3.413e-2, ratio 0.68)
-- decode-1 (T=1, 0.3s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   layer_out.47   rms_rel 2.173e-1 cos 0.976431  (HF bf16 2.624e-1, ratio 0.83)
   mixer_out      rms_rel 2.192e-1 cos 0.976550  (HF bf16 2.595e-1, ratio 0.84)
   logits         rms_rel 2.013e-1 cos 0.979672  (HF bf16 2.381e-1, ratio 0.85)
   top-1 vs fp32: ours 1/1, HF bf16 1/1; |dlogprob| on fp32 top-20: ours max 1.560 mean 0.5845, HF bf16 max 3.372 mean 0.8829
   worst layer_out vs HF bf16 budget: layer_out.36 (rms_rel 2.378e-1, ratio 1.10)
-- decode-2 (T=1, 0.3s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   layer_out.47   rms_rel 1.097e-1 cos 0.994006  (HF bf16 1.259e-1, ratio 0.87)
   mixer_out      rms_rel 9.796e-2 cos 0.995259  (HF bf16 1.188e-1, ratio 0.82)
   logits         rms_rel 1.078e-1 cos 0.994456  (HF bf16 1.153e-1, ratio 0.93)
   top-1 vs fp32: ours 1/1, HF bf16 1/1; |dlogprob| on fp32 top-20: ours max 1.349 mean 0.2780, HF bf16 max 1.292 mean 0.3826
   worst layer_out vs HF bf16 budget: layer_out.29 (rms_rel 6.695e-2, ratio 1.23)
-- decode-3 (T=1, 0.3s)
   residual_in    rms_rel 0.000e0 cos 1.000000  (HF bf16 0.000e0, ratio NaN)
   layer_out.47   rms_rel 7.814e-2 cos 0.996996  (HF bf16 9.300e-2, ratio 0.84)
   mixer_out      rms_rel 6.307e-2 cos 0.998128  (HF bf16 8.182e-2, ratio 0.77)
   logits         rms_rel 7.034e-2 cos 0.997531  (HF bf16 8.296e-2, ratio 0.85)
   top-1 vs fp32: ours 1/1, HF bf16 1/1; |dlogprob| on fp32 top-20: ours max 0.541 mean 0.1542, HF bf16 max 0.430 mean 0.1997
   worst layer_out vs HF bf16 budget: layer_out.29 (rms_rel 8.529e-2, ratio 1.18)
```
