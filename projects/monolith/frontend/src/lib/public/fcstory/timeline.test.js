import { describe, it, expect } from "vitest";
import {
  PHASES,
  clamp,
  lerp,
  sub,
  easeInOut,
  easeOut,
  coldSegments,
  restoreBarFraction,
  captionOpacity,
  MIN_SEGMENT_FRACTION,
} from "./timeline.js";
import { cold, restores } from "./data/trace.js";

describe("fcstory timeline phase windows", () => {
  const order = ["heroOut", "build", "freeze", "restore", "repeat", "out"];

  it("are ordered and non-overlapping, within [0,1]", () => {
    // The mockup's windows have deliberate gaps between beats (e.g. heroOut
    // ends at 0.07, build starts at 0.08), so this asserts monotonic
    // non-overlap rather than strict contiguity.
    let prevEnd = -Infinity;
    for (const name of order) {
      const [a, b] = PHASES[name];
      expect(a).toBeGreaterThanOrEqual(0);
      expect(b).toBeLessThanOrEqual(1);
      expect(b).toBeGreaterThan(a);
      expect(a).toBeGreaterThanOrEqual(prevEnd);
      prevEnd = b;
    }
  });

  it("spans from 0 to 1 overall", () => {
    expect(PHASES.heroOut[0]).toBe(0);
    expect(PHASES.out[1]).toBe(1);
  });
});

describe("fcstory timeline easing", () => {
  it("easeInOut and easeOut map the boundaries 0->0 and 1->1", () => {
    expect(easeInOut(0)).toBeCloseTo(0, 10);
    expect(easeInOut(1)).toBeCloseTo(1, 10);
    expect(easeOut(0)).toBeCloseTo(0, 10);
    expect(easeOut(1)).toBeCloseTo(1, 10);
  });

  it("easeInOut is symmetric around its midpoint", () => {
    expect(easeInOut(0.5)).toBeCloseTo(0.5, 10);
  });

  it("lerp and sub behave at their endpoints", () => {
    expect(lerp(10, 20, 0)).toBe(10);
    expect(lerp(10, 20, 1)).toBe(20);
    expect(sub(0.2, 0.2, 0.4)).toBe(0);
    expect(sub(0.4, 0.2, 0.4)).toBe(1);
    expect(sub(0.3, 0.2, 0.4)).toBeCloseTo(0.5);
  });

  it("clamp bounds its input", () => {
    expect(clamp(-1, 0, 1)).toBe(0);
    expect(clamp(2, 0, 1)).toBe(1);
    expect(clamp(0.5, 0, 1)).toBe(0.5);
  });
});

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

describe("captionOpacity", () => {
  it("is 0 before the window opens and after it closes", () => {
    const { opacity: before } = captionOpacity(0.0, 0.3, 0.5);
    const { opacity: after } = captionOpacity(1.0, 0.3, 0.5);
    expect(before).toBeLessThanOrEqual(0);
    expect(after).toBeLessThanOrEqual(0);
  });

  it("is fully visible in the middle of the window", () => {
    const { opacity } = captionOpacity(0.4, 0.3, 0.5);
    expect(opacity).toBeCloseTo(1, 5);
  });
});
