// The GPU's one host-to-device copy engine runs ready copies in submission
// order across streams (ARCHITECTURE.md, "Why staging copies trickle").
// A layer's stage-ahead is about 450 records, 1.2 GB and 45 ms of engine time.
export const STAGE_RECORDS = 450;
export const STAGE_GB = 1.2;
export const STAGE_MS = 45;
export const RECORD_MS = STAGE_MS / STAGE_RECORDS;
export const PIECE_RECORDS = 8;

// Measured, warm prefill, decode unchanged (same section).
export const results = [
  { tokens: "32k", before: 10.3, after: 9.4 },
  { tokens: "95k", before: 28.1, after: 27.4 },
];
export const idleBefore = { idle: 2.7, total: 10.5 };

// Schematic: when the computing layer needs its own fetch copies, and how many.
export const ARRIVE_MS = 4.3;
export const FETCH_RECORDS = 10;

// Simulates the copy engine. "all" submits the whole stage-ahead at once;
// "trickle" submits it PIECE_RECORDS at a time as earlier pieces complete, so
// the fetch copies, arriving at ARRIVE_MS, queue behind at most one piece.
export function simulate(mode, arrive = ARRIVE_MS, fetch = FETCH_RECORDS) {
  const blocks = [];
  const fetchMs = fetch * RECORD_MS;
  let t = 0;
  let fetchStart;
  if (mode === "all") {
    blocks.push({ kind: "stage", start: 0, end: STAGE_MS });
    t = STAGE_MS;
    fetchStart = Math.max(t, arrive);
  } else {
    let left = STAGE_RECORDS;
    while (left > 0) {
      if (fetchStart === undefined && t >= arrive) {
        fetchStart = t;
        t += fetchMs;
        continue;
      }
      const n = Math.min(PIECE_RECORDS, left);
      blocks.push({ kind: "stage", start: t, end: t + n * RECORD_MS });
      t += n * RECORD_MS;
      left -= n;
    }
    if (fetchStart === undefined) fetchStart = Math.max(t, arrive);
  }
  blocks.push({ kind: "fetch", start: fetchStart, end: fetchStart + fetchMs });
  const end = Math.max(...blocks.map((b) => b.end));
  return { blocks, wait: fetchStart - arrive, end };
}
