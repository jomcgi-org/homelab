# GPU spike: Qwen 3.8 Flash layer 0 (2026-10-04)

`oominf check-layer` runs decoder layer 0 (hyper-connections, Gated DeltaNet,
router, shared expert, NVFP4 routed experts) on the RTX 4090 from the converted
`.oom` model and compares every stage against the HF `qwen4_exp` fp32 fixtures
(`reference/`), for a 73-token prefill and three single-token decode steps.

**Result: every stage is within budget in both modes.** Budget is the
reference's own worst bf16-vs-fp32 error for that stage across the four steps;
the worst ratio is 0.73.

- **Isolated** (each stage fed exact reference inputs): the fp32 stages (conv,
  delta rule, gated norm, routed experts, combines) match HF fp32 to about 1e-7;
  stages behind a bf16 GEMM sit at 0.1x to 0.7x of HF's own bf16 error. Top-k
  expert sets match on every token.
- **Chained** (whole layer from the layer input, state carried across steps):
  layer output at 0.23x to 0.30x of budget; 3 of 73 prefill tokens pick a
  different expert set at near-ties (HF's own bf16 run flips 10 of 73).

## Precision as implemented

Per the precision rule in #6825: dense GEMMs use bf16 operands (the released
weight dtype; activations rounded to bf16 for tensor cores) with fp32
accumulation and fp32 outputs. Everything else is fp32: residual stream,
norms, conv, delta-rule state, router softmax. Routed experts are NVFP4
dequantised exactly to fp32 and run as fp32 GEMMs (W4A16, no activation
quantisation).

## Not yet

- Speed: these are reference kernels (the delta-rule kernel spills its state to
  local memory; experts are dequantised per call). Performance work comes after
  correctness for the full forward pass.
- Layer 1 (PLE) and full-attention layers (sparse indexer) are not implemented.

## Run

    oominf check-layer \
      --model /disks/nvme-02/src/oominf-data/models/qwen38-flash-nvfp4.oom \
      --fixtures /disks/nvme-02/src/oominf-data/fixtures/qwen38-flash/layer-000

Needs the GPU free (stop `freetoken-serve` and `freetoken-keepwarm.timer`).
Exits non-zero if any isolated stage exceeds 1.5x budget.

## Full output

```
layer 0 loaded in 0.7s; mode w4a16; truth = HF fp32; budget* = worst HF bf16 vs fp32 over steps

== isolated (each stage fed exact reference inputs) ==
-- prefill (T=73)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          6.466e-4   0.99999979     2.499e-3    0.26
   attn_hc.inject         3.243e-4   0.99999995     5.835e-3    0.06
   gdn.in_proj_qkv        7.373e-4   0.99999973     2.567e-3    0.29
   gdn.in_proj_z          6.233e-4   0.99999981     2.366e-3    0.26
   gdn.in_proj_b          4.243e-4   0.99999991     2.063e-3    0.21
   gdn.in_proj_a          4.448e-4   0.99999990     2.576e-3    0.17
   gdn.conv_out           4.070e-8   1.00000000     2.923e-3    0.00
   gdn.core_out           2.657e-7   1.00000000     3.926e-3    0.00
   gdn.norm_out           7.199e-8   1.00000000     5.123e-3    0.00
   mixer_out              1.437e-3   0.99999897     4.977e-3    0.29
   attn_combine_out       2.769e-8   1.00000000     7.538e-3    0.00
   mlp_hc.mixed           1.294e-3   0.99999916     9.003e-3    0.14
   mlp_hc.inject          2.625e-4   0.99999997     4.447e-3    0.06
   router_logits          2.049e-4   0.99999998     2.136e-3    0.10
   topk_ids                      0 tokens with a different expert set (of 73)
   topk_weights           9.147e-8   1.00000000     7.166e-3    0.00
   shared_out             1.001e-3   0.99999950     3.812e-3    0.26
   shared_gate_logit      4.806e-4   0.99999988     2.220e-2    0.02
   routed_out             2.644e-7   1.00000000     2.140e-2    0.00
   moe_out                5.088e-8   1.00000000     1.043e-2    0.00
   layer_out              3.727e-8   1.00000000     8.269e-3    0.00
   state.conv              0.000e0   1.00000000     2.167e-3    0.00
   state.recurrent        3.052e-7   1.00000000     4.676e-3    0.00
-- decode-1 (T=1)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          1.020e-3   0.99999950     2.499e-3    0.41
   attn_hc.inject         6.976e-4   1.00000000     5.835e-3    0.12
   gdn.in_proj_qkv        5.880e-4   0.99999986     2.567e-3    0.23
   gdn.in_proj_z          5.879e-4   0.99999987     2.366e-3    0.25
   gdn.in_proj_b          3.113e-4   0.99999996     2.063e-3    0.15
   gdn.in_proj_a          5.205e-4   0.99999992     2.576e-3    0.20
   gdn.conv_out           2.965e-8   1.00000000     2.923e-3    0.00
   gdn.core_out           2.232e-7   1.00000000     3.926e-3    0.00
   gdn.norm_out           6.092e-8   1.00000000     5.123e-3    0.00
   mixer_out              1.340e-3   0.99999911     4.977e-3    0.27
   attn_combine_out       2.683e-8   1.00000000     7.538e-3    0.00
   mlp_hc.mixed           1.194e-3   0.99999929     9.003e-3    0.13
   mlp_hc.inject          5.252e-4   1.00000000     4.447e-3    0.12
   router_logits          2.390e-4   0.99999999     2.136e-3    0.11
   topk_ids                      0 tokens with a different expert set (of 1)
   topk_weights           1.607e-8   1.00000000     7.166e-3    0.00
   shared_out             9.206e-4   0.99999958     3.812e-3    0.24
   shared_gate_logit      1.661e-3   1.00000000     2.220e-2    0.07
   routed_out             3.129e-7   1.00000000     2.140e-2    0.00
   moe_out                3.772e-8   1.00000000     1.043e-2    0.00
   layer_out              3.463e-8   1.00000000     8.269e-3    0.00
   state.conv              0.000e0   1.00000000     2.167e-3    0.00
   state.recurrent        7.963e-8   1.00000000     4.676e-3    0.00
-- decode-2 (T=1)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          3.956e-4   0.99999994     2.499e-3    0.16
   attn_hc.inject         1.476e-4   0.99999999     5.835e-3    0.03
   gdn.in_proj_qkv        5.651e-4   0.99999984     2.567e-3    0.22
   gdn.in_proj_z          4.985e-4   0.99999988     2.366e-3    0.21
   gdn.in_proj_b          1.704e-4   0.99999999     2.063e-3    0.08
   gdn.in_proj_a          3.436e-4   0.99999994     2.576e-3    0.13
   gdn.conv_out           3.999e-8   1.00000000     2.923e-3    0.00
   gdn.core_out           1.222e-7   1.00000000     3.926e-3    0.00
   gdn.norm_out           7.741e-8   1.00000000     5.123e-3    0.00
   mixer_out              1.290e-3   0.99999921     4.977e-3    0.26
   attn_combine_out       2.811e-8   1.00000000     7.538e-3    0.00
   mlp_hc.mixed           1.681e-3   0.99999860     9.003e-3    0.19
   mlp_hc.inject          2.356e-4   1.00000000     4.447e-3    0.05
   router_logits          2.202e-4   0.99999998     2.136e-3    0.10
   topk_ids                      0 tokens with a different expert set (of 1)
   topk_weights           3.909e-8   1.00000000     7.166e-3    0.00
   shared_out             7.993e-4   0.99999972     3.812e-3    0.21
   shared_gate_logit      1.882e-4   1.00000000     2.220e-2    0.01
   routed_out             3.199e-7   1.00000000     2.140e-2    0.00
   moe_out                3.663e-8   1.00000000     1.043e-2    0.00
   layer_out              3.862e-8   1.00000000     8.269e-3    0.00
   state.conv              0.000e0   1.00000000     2.167e-3    0.00
   state.recurrent        6.688e-8   1.00000000     4.676e-3    0.00
-- decode-3 (T=1)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          1.376e-3   0.99999905     2.499e-3    0.55
   attn_hc.inject         1.626e-4   1.00000000     5.835e-3    0.03
   gdn.in_proj_qkv        7.409e-4   0.99999976     2.567e-3    0.29
   gdn.in_proj_z          5.992e-4   0.99999985     2.366e-3    0.25
   gdn.in_proj_b          5.119e-4   0.99999994     2.063e-3    0.25
   gdn.in_proj_a          5.677e-4   0.99999991     2.576e-3    0.22
   gdn.conv_out           3.706e-8   1.00000000     2.923e-3    0.00
   gdn.core_out           1.688e-7   1.00000000     3.926e-3    0.00
   gdn.norm_out           7.910e-8   1.00000000     5.123e-3    0.00
   mixer_out              1.249e-3   0.99999923     4.977e-3    0.25
   attn_combine_out       2.897e-8   1.00000000     7.538e-3    0.00
   mlp_hc.mixed           1.101e-3   0.99999939     9.003e-3    0.12
   mlp_hc.inject          4.550e-5   1.00000000     4.447e-3    0.01
   router_logits          2.043e-4   0.99999998     2.136e-3    0.10
   topk_ids                      0 tokens with a different expert set (of 1)
   topk_weights           1.294e-7   1.00000000     7.166e-3    0.00
   shared_out             9.893e-4   0.99999958     3.812e-3    0.26
   shared_gate_logit      3.821e-4   1.00000000     2.220e-2    0.02
   routed_out             2.338e-7   1.00000000     2.140e-2    0.00
   moe_out                3.868e-8   1.00000000     1.043e-2    0.00
   layer_out              3.472e-8   1.00000000     8.269e-3    0.00
   state.conv              0.000e0   1.00000000     2.167e-3    0.00
   state.recurrent        7.711e-8   1.00000000     4.676e-3    0.00

== chained (whole layer from residual_in) ==
-- prefill (T=73)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          6.466e-4   0.99999979     2.499e-3    0.26
   attn_hc.inject         3.243e-4   0.99999995     5.835e-3    0.06
   gdn.in_proj_qkv        9.916e-4   0.99999952     2.567e-3    0.39
   gdn.in_proj_z          8.635e-4   0.99999963     2.366e-3    0.37
   gdn.in_proj_b          7.478e-4   0.99999972     2.063e-3    0.36
   gdn.in_proj_a          6.415e-4   0.99999980     2.576e-3    0.25
   gdn.conv_out           8.379e-4   0.99999966     2.923e-3    0.29
   gdn.core_out           7.033e-4   0.99999977     3.926e-3    0.18
   gdn.norm_out           1.141e-3   0.99999936     5.123e-3    0.22
   mixer_out              1.959e-3   0.99999809     4.977e-3    0.39
   attn_combine_out       1.989e-3   0.99999803     7.538e-3    0.26
   mlp_hc.mixed           3.090e-3   0.99999523     9.003e-3    0.34
   mlp_hc.inject          6.392e-4   0.99999980     4.447e-3    0.14
   router_logits          4.139e-4   0.99999992     2.136e-3    0.19
   topk_ids                      3 tokens with a different expert set (of 73)
   topk_weights           1.567e-3   0.99999878     7.166e-3    0.22
   shared_out             1.427e-3   0.99999898     3.812e-3    0.37
   shared_gate_logit      1.089e-3   0.99999941     2.220e-2    0.05
   routed_out             1.258e-2   0.99992082     2.140e-2    0.59
   moe_out                5.783e-3   0.99998329     1.043e-2    0.55
   layer_out              2.446e-3   0.99999701     8.269e-3    0.30
   state.conv             1.239e-3   0.99999949     2.167e-3    0.57
   state.recurrent        1.442e-3   0.99999899     4.676e-3    0.31
-- decode-1 (T=1)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          1.020e-3   0.99999950     2.499e-3    0.41
   attn_hc.inject         6.976e-4   1.00000000     5.835e-3    0.12
   gdn.in_proj_qkv        9.420e-4   0.99999960     2.567e-3    0.37
   gdn.in_proj_z          1.032e-3   0.99999957     2.366e-3    0.44
   gdn.in_proj_b          7.682e-4   0.99999972     2.063e-3    0.37
   gdn.in_proj_a          7.221e-4   0.99999986     2.576e-3    0.28
   gdn.conv_out           6.894e-4   0.99999977     2.923e-3    0.24
   gdn.core_out           8.618e-4   0.99999973     3.926e-3    0.22
   gdn.norm_out           1.197e-3   0.99999947     5.123e-3    0.23
   mixer_out              1.833e-3   0.99999874     4.977e-3    0.37
   attn_combine_out       1.641e-3   0.99999867     7.538e-3    0.22
   mlp_hc.mixed           2.698e-3   0.99999637     9.003e-3    0.30
   mlp_hc.inject          1.411e-4   1.00000000     4.447e-3    0.03
   router_logits          3.588e-4   0.99999994     2.136e-3    0.17
   topk_ids                      0 tokens with a different expert set (of 1)
   topk_weights           6.567e-4   0.99999989     7.166e-3    0.09
   shared_out             1.062e-3   0.99999954     3.812e-3    0.28
   shared_gate_logit      3.925e-3   1.00000000     2.220e-2    0.18
   routed_out             2.266e-3   0.99999765     2.140e-2    0.11
   moe_out                1.230e-3   0.99999926     1.043e-2    0.12
   layer_out              1.129e-3   0.99999937     8.269e-3    0.14
   state.conv             1.207e-3   0.99999938     2.167e-3    0.56
   state.recurrent        1.370e-3   0.99999906     4.676e-3    0.29
-- decode-2 (T=1)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          3.956e-4   0.99999994     2.499e-3    0.16
   attn_hc.inject         1.476e-4   0.99999999     5.835e-3    0.03
   gdn.in_proj_qkv        5.952e-4   0.99999983     2.567e-3    0.23
   gdn.in_proj_z          5.241e-4   0.99999987     2.366e-3    0.22
   gdn.in_proj_b          2.412e-4   0.99999998     2.063e-3    0.12
   gdn.in_proj_a          3.436e-4   0.99999996     2.576e-3    0.13
   gdn.conv_out           3.420e-4   0.99999994     2.923e-3    0.12
   gdn.core_out           6.604e-4   0.99999993     3.926e-3    0.17
   gdn.norm_out           6.912e-4   0.99999977     5.123e-3    0.13
   mixer_out              1.580e-3   0.99999890     4.977e-3    0.32
   attn_combine_out       1.640e-3   0.99999880     7.538e-3    0.22
   mlp_hc.mixed           2.636e-3   0.99999653     9.003e-3    0.29
   mlp_hc.inject          1.452e-4   0.99999999     4.447e-3    0.03
   router_logits          3.384e-4   0.99999995     2.136e-3    0.16
   topk_ids                      0 tokens with a different expert set (of 1)
   topk_weights           2.337e-3   0.99999728     7.166e-3    0.33
   shared_out             9.340e-4   0.99999961     3.812e-3    0.24
   shared_gate_logit      1.150e-3   1.00000000     2.220e-2    0.05
   routed_out             2.197e-3   0.99999810     2.140e-2    0.10
   moe_out                1.407e-3   0.99999903     1.043e-2    0.13
   layer_out              1.269e-3   0.99999923     8.269e-3    0.15
   state.conv             1.014e-3   0.99999951     2.167e-3    0.47
   state.recurrent        1.324e-3   0.99999913     4.676e-3    0.28
-- decode-3 (T=1)
   stage                   rms_rel       cosine      budget*   ratio
   attn_hc.mixed          1.376e-3   0.99999905     2.499e-3    0.55
   attn_hc.inject         1.626e-4   1.00000000     5.835e-3    0.03
   gdn.in_proj_qkv        1.872e-3   0.99999881     2.567e-3    0.73
   gdn.in_proj_z          1.616e-3   0.99999929     2.366e-3    0.68
   gdn.in_proj_b          1.308e-3   0.99999918     2.063e-3    0.63
   gdn.in_proj_a          1.289e-3   0.99999974     2.576e-3    0.50
   gdn.conv_out           1.520e-3   0.99999946     2.923e-3    0.52
   gdn.core_out           9.465e-4   0.99999958     3.926e-3    0.24
   gdn.norm_out           2.003e-3   0.99999910     5.123e-3    0.39
   mixer_out              2.688e-3   0.99999832     4.977e-3    0.54
   attn_combine_out       2.614e-3   0.99999757     7.538e-3    0.35
   mlp_hc.mixed           2.927e-3   0.99999575     9.003e-3    0.33
   mlp_hc.inject          9.199e-4   1.00000000     4.447e-3    0.21
   router_logits          3.437e-4   0.99999994     2.136e-3    0.16
   topk_ids                      0 tokens with a different expert set (of 1)
   topk_weights           1.935e-3   0.99999814     7.166e-3    0.27
   shared_out             1.308e-3   0.99999930     3.812e-3    0.34
   shared_gate_logit      8.749e-4   1.00000000     2.220e-2    0.04
   routed_out             1.247e-3   0.99999922     2.140e-2    0.06
   moe_out                1.252e-3   0.99999940     1.043e-2    0.12
   layer_out              1.532e-3   0.99999919     8.269e-3    0.19
   state.conv             1.298e-3   0.99999924     2.167e-3    0.60
   state.recurrent        1.553e-3   0.99999886     4.676e-3    0.33
```
