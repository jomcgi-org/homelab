// Numbers behind the 4090 post's interactive figures. Each one cites where it
// was measured or how it is derived; the post's fallback tables repeat them and
// data.test.js keeps the two in step.

// One converted decoder expert: weights and scales for one expert, aligned
// (oominf convert log: "512 records x 2768896 bytes").
export const RECORD_BYTES = 2_768_896;

// Where a routed expert is when a token needs it (ARCHITECTURE.md, "Why host
// compute"). The NVMe row is arithmetic, not an engine measurement.
export const expertPaths = [
  {
    key: "vram",
    label: "In VRAM",
    micros: 0,
    cost: "No transfer",
    moves: "Nothing",
    detail: "Already in our hot cache! No transfer penalty.",
  },
  {
    key: "cpu",
    label: "Host hit, run on CPU",
    micros: 70,
    cost: "About 70 µs",
    moves: "A 10 KB output row",
    detail:
      "During one-token decode, 8 CPU cores compute it while the GPU runs the resident experts.",
  },
  {
    key: "copy",
    label: "Host hit, copied",
    micros: 110,
    cost: "About 110 µs",
    moves: "2.7 MB over PCIe",
    detail:
      "The GPU waits for the copy. Experts that keep recurring are kept in VRAM.",
  },
  {
    key: "nvme",
    label: "NVMe miss",
    micros: 560,
    cost: "About 0.45 ms, then the copy",
    moves: "2.7 MB from disk, then over PCIe",
    illustrative: true,
  },
];
