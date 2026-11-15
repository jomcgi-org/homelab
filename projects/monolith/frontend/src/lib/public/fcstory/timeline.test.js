import { describe, it, expect } from "vitest";
import {
  coldSegments,
  restoreBarFraction,
  MIN_SEGMENT_FRACTION,
} from "./timeline.js";
import { cold, restores } from "./data/trace.js";

describe("baked trace data sanity", () => {
  it("has a cold run with at least 3 phases", () => {
    expect(cold.phases.length).toBeGreaterThanOrEqual(3);
  });

  it("has at least 8 restore runs", () => {
    expect(restores.length).toBeGreaterThanOrEqual(8);
  });

  it("every restore run includes a snapshot_restore phase", () => {
    for (const run of restores) {
      expect(run.phases.some((p) => p.name === "snapshot_restore")).toBe(true);
    }
  });
});

describe("coldSegments", () => {
  it("returns one segment per cold phase, proportional to cold.total", () => {
    const segs = coldSegments(cold);
    expect(segs).toHaveLength(cold.phases.length);
    const boot = segs.find((s) => s.name === "firecracker_boot");
    const wait = segs.find((s) => s.name === "guest_wait_ready");
    const save = segs.find((s) => s.name === "snapshot_save");
    expect(boot).toBeDefined();
    expect(wait).toBeDefined();
    expect(save).toBeDefined();

    // wait + save + boot durations sum to the full cold total (the real
    // trace has no other phases in this run).
    const totalMs = segs.reduce((sum, s) => sum + s.ms, 0);
    expect(totalMs).toBeCloseTo(cold.total, 1);
  });

  it("gives the tiny boot phase a minimum-width escape hatch", () => {
    const segs = coldSegments(cold);
    const boot = segs.find((s) => s.name === "firecracker_boot");
    // firecracker_boot is 15.1ms of a 9264ms total: its raw fraction
    // (~0.0016) is well under the floor, so the floor must be what wins.
    const rawFraction = boot.ms / cold.total;
    expect(rawFraction).toBeLessThan(MIN_SEGMENT_FRACTION);
    expect(boot.fraction).toBe(MIN_SEGMENT_FRACTION);
    expect(boot.fraction).toBeGreaterThan(rawFraction);
  });

  it("segments not hitting the floor stay proportional (no floor distortion)", () => {
    const segs = coldSegments(cold);
    const wait = segs.find((s) => s.name === "guest_wait_ready");
    expect(wait.fraction).toBeCloseTo(wait.ms / cold.total, 10);
  });
});

describe("restoreBarFraction", () => {
  it("matches restore.total / cold.total for a representative run", () => {
    const run = restores[0];
    const fraction = restoreBarFraction(run, cold);
    expect(fraction).toBeCloseTo(run.total / cold.total, 10);
  });

  it("applies the same minimum-width floor as coldSegments", () => {
    for (const run of restores) {
      const raw = run.total / cold.total;
      const fraction = restoreBarFraction(run, cold);
      expect(fraction).toBeGreaterThanOrEqual(MIN_SEGMENT_FRACTION);
      if (raw < MIN_SEGMENT_FRACTION) {
        expect(fraction).toBe(MIN_SEGMENT_FRACTION);
      } else {
        expect(fraction).toBeCloseTo(raw, 10);
      }
    }
  });
});
