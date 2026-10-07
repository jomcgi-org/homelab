// Text-model weights of the pinned release, from
// projects/oom-inference/bench/release_inventory.py (decimal GB, vision
// excluded), and where the engine keeps each part (ARCHITECTURE.md, FORMAT.md).
export const weights = [
  {
    key: "experts",
    label: "Decoder experts",
    gb: 67.95,
    tone: "gpu",
    where:
      "48 layers × 512 experts, one 2.7 MB record each. Hot records sit in VRAM, warm ones in pinned RAM, all of them on NVMe.",
  },
  {
    key: "ple",
    label: "PLE tables",
    gb: 51.2,
    tone: "disk",
    where:
      "FP8 n-gram tables. They stay on NVMe; buffered reads gather the rows a prompt needs.",
  },
  {
    key: "dense",
    label: "Dense",
    gb: 10.08,
    tone: "ram",
    where:
      "Everything that is not an expert or a PLE table. Loaded into VRAM at start-up.",
  },
  {
    key: "mtp",
    label: "MTP experts",
    gb: 5.03,
    tone: "cache",
    where:
      "The draft head's 512 BF16 experts. Their own small tier: one VRAM chunk, records in pinned RAM from start-up.",
  },
];

export const capacities = [
  { key: "vram", label: "VRAM", gb: 24 },
  { key: "ram", label: "Host RAM", gb: 64 },
  { key: "both", label: "VRAM + RAM", gb: 88 },
];

export const totalGb = (parts = weights) =>
  Math.round(parts.reduce((sum, p) => sum + p.gb, 0) * 100) / 100;

// Width of `gb` on the shared scale, as a percentage of the largest bar.
export const widthPct = (gb, max = totalGb()) => (gb / max) * 100;
