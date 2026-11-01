// Published decode rates for the same model family on a 96 GB RTX PRO 6000,
// with the 4090 replay for scale. Rows come from the post's section 9 table,
// which keeps the sources; they are author-reported scale references, not a
// controlled ranking.
export const published = [
  {
    key: "vllm",
    label: "vLLM, NVFP4",
    from: 74.4,
    to: 74.4,
    speculation: false,
  },
  {
    key: "sglang-off",
    label: "SGLang, paired, MTP off",
    from: 97.9,
    to: 97.9,
    speculation: false,
  },
  {
    key: "sglang-on",
    label: "SGLang, paired, tuned",
    from: 144.6,
    to: 144.6,
    speculation: true,
  },
  {
    key: "sglang-later",
    label: "SGLang, later stack",
    from: 179.4,
    to: 192.7,
    speculation: true,
  },
  {
    key: "llama",
    label: "llama.cpp, UD-Q4_K_XL",
    from: 59.7,
    to: 59.7,
    speculation: false,
  },
  {
    key: "oom",
    label: "oom-inference, 4090",
    from: 45.4,
    to: 45.4,
    speculation: true,
    ours: true,
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
