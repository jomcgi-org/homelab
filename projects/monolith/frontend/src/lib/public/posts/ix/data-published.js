// Published decode rates for the same model family on a 96 GB RTX PRO 6000,
// with the 4090 replay for scale. Rows, links and caveats come from the post's
// section 9 table and the paragraph under it; they are author-reported scale
// references, not a controlled ranking.
export const published = [
  {
    key: "vllm",
    label: "vLLM, NVFP4",
    from: 74.4,
    to: 74.4,
    speculation: false,
    url: "https://huggingface.co/primitive-ai/Qwen3.8-Flash-Next-NVFP4",
    caveat:
      "8k input, 512 output, cache-free. Keeps a roughly 95 GB BF16 PLE table in host RAM.",
  },
  {
    key: "sglang-off",
    label: "SGLang, paired, MTP off",
    from: 97.9,
    to: 97.9,
    speculation: false,
    url: "https://github.com/lEWFkRAD/qwen38-rtx-pro-6000/blob/59f59a86245bc63ff7f74d775b890a0bd9888734/flash-next-w4-ple/docs/CACHED-NEXTN-REPLAYSSM-PRODUCTION-ADDENDUM-2026-09-01.md",
    caveat: "NVFP4 + W4 PLE, 512 output, warm, one request at a time.",
  },
  {
    key: "sglang-on",
    label: "SGLang, paired, tuned",
    from: 144.6,
    to: 144.6,
    speculation: true,
    url: "https://github.com/lEWFkRAD/qwen38-rtx-pro-6000/blob/59f59a86245bc63ff7f74d775b890a0bd9888734/flash-next-w4-ple/docs/CACHED-NEXTN-REPLAYSSM-PRODUCTION-ADDENDUM-2026-09-01.md",
    caveat:
      "Same campaign with MTP and other tuning. KV capacity, memory settings and idle conditions also changed, so not all of the gain is MTP.",
  },
  {
    key: "sglang-later",
    label: "SGLang, later stack",
    from: 179.4,
    to: 192.7,
    speculation: true,
    url: "https://github.com/SSHdotCodes/qwen-3.8-flash-next-pro6000/blob/8e5be4440eeea37d31994b0845d1344201de7741/results/20260924/REPORT.md",
    caveat:
      "Four workloads, 8,192 output tokens: long reasoning outputs and different sampling.",
  },
  {
    key: "llama",
    label: "llama.cpp, UD-Q4_K_XL",
    from: 59.7,
    to: 59.7,
    speculation: false,
    url: "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/discussions/3",
    caveat:
      "22.7k-token prompt. Different weight, PLE and KV choices from the other rows.",
  },
  {
    key: "oom",
    label: "oom-inference, 4090",
    from: 45.4,
    to: 45.4,
    speculation: true,
    ours: true,
    url: "",
    caveat:
      "The research replay: 21,878-token prompt, warm tiers, fresh prefix. MTP and prompt lookup. 24 GB VRAM, 64 GB RAM, NVMe.",
  },
];

export const SCALE_MAX = 200;
export const ticks = [0, 50, 100, 150, 200];

export const pct = (v) => (v / SCALE_MAX) * 100;

export function rate(row) {
  return row.from === row.to
    ? `${row.from.toFixed(1)} tok/s`
    : `${row.from.toFixed(1)}–${row.to.toFixed(1)} tok/s`;
}
