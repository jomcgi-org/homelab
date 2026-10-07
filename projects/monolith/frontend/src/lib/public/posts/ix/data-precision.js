// Runtime precision settings, from ARCHITECTURE.md ("Why k8v6 by default"
// and the opt-in settings after it). Each was measured on its own against the
// exact reference; the effects do not add.
export const settings = [
  {
    key: "k8v6",
    label: "k8v6 KV cache",
    flag: "--kv-cache k8v6",
    status: "Default",
    changes: "8-bit keys and 6-bit values in the KV cache.",
    gain: "About 3x less KV memory; decode +19% at long context",
    gainPercent: 19,
    gainPhase: "decode",
    slots: "About 1,400 more expert slots at 95k tokens",
    accuracy: "Inside the model's own rounding noise",
    kl: null,
    top1: null,
    ref: "#6830",
  },
  {
    key: "dense",
    label: "FP8 dense",
    flag: "--dense fp8",
    status: "Opt-in",
    changes: "Dense weights stored as FP8, one scale per 128 weights.",
    gain: "Verify step 35–37 ms against 43–50 ms at 32k–95k",
    gainPercent: 23,
    gainPhase: "decode step",
    slots: "About 1,660 more expert slots",
    accuracy: "About 3x the rounding floor",
    kl: [0.1, 0.13],
    top1: "86–88%",
    ref: "#6865",
  },
  {
    key: "expert",
    label: "BF16 expert prefill",
    flag: "--expert-precision bf16",
    status: "Opt-in",
    changes:
      "Prefill expert activations rounded to BF16 once, not split into three terms.",
    gain: "Warm 32k prefill 16.2 s to 14.0 s",
    gainPercent: 14,
    gainPhase: "prefill",
    slots: null,
    accuracy: "About at the rounding floor",
    kl: [0.058, 0.058],
    top1: "92.1%",
    ref: "#6838",
  },
  {
    key: "attention",
    label: "BF16 attention prefill",
    flag: "--attention-precision bf16",
    status: "Opt-in",
    changes: "Prefill attention on BF16 tensor cores; decode stays FP32.",
    gain: "Prefill attention 1.51 s to 1.27 s per 32k prompt",
    gainPercent: 16,
    gainPhase: "prefill attention",
    slots: null,
    accuracy: "Inside the rounding floor",
    kl: [0.051, 0.051],
    top1: "92.6%",
    ref: null,
  },
];

// Speed bar: share of the named phase's time saved (or decode rate gained,
// for k8v6). FP8 dense uses the midpoints, 36 ms against 46.5 ms.
export function percentSaved(before, after) {
  return Math.round((1 - after / before) * 100);
}

export const KL_SCALE = 0.15;
