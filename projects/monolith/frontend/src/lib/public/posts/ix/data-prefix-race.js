// First token for a new question on the same 32k-token documents, max-perf
// config (#6859; ARCHITECTURE.md "Prefix checkpoints").
export const runs = [
  {
    key: "prefill",
    label: "Full prefill",
    seconds: 13.1,
    detail: "No reuse: reads the whole 32k-token prompt.",
  },
  {
    key: "stored",
    label: "Stored entry",
    seconds: 0.95,
    detail:
      "Restores a 1.1 GB entry from the prefix store, then reads the rest.",
  },
  {
    key: "live",
    label: "Live checkpoint",
    seconds: 0.64,
    detail: "Rewinds the live sequence to its checkpoint, then reads the rest.",
  },
];

export const REUSED = "Both reuse 32,531 of about 32,549 prompt tokens";
export const SPEED = 4;

// Progress of a run at clock time t (seconds of real engine time), 0..1.
export function progress(run, t) {
  return Math.max(0, Math.min(1, t / run.seconds));
}
