import { describe, expect, it } from "vitest";
import {
  classifyTier,
  includedSnapshotWait,
  parkedMsBreakdown,
  phaseLabel,
  shouldRetry,
} from "./console-retry.js";

describe("classifyTier", () => {
  it.each([
    ["warm", "warm"],
    ["relight", "relight"],
    ["cold", "cold"],
  ])("classifies %s", (classification, expected) => {
    expect(classifyTier(classification)).toBe(expected);
  });

  it.each(["transitional", "unknown", "unrecognized", undefined, null])(
    "leaves %s unclear",
    (classification) => {
      expect(classifyTier(classification)).toBeNull();
    },
  );
});

describe("shouldRetry", () => {
  it("allows a retry well within the window", () => {
    expect(shouldRetry(500, 30000)).toBe(true);
  });

  it("allows retries late in the attempt sequence while in the window", () => {
    expect(shouldRetry(29000, 30000)).toBe(true);
    expect(shouldRetry(15000, 30000)).toBe(true);
  });

  it("stops at and after the window boundary", () => {
    expect(shouldRetry(30000, 30000)).toBe(false);
    expect(shouldRetry(31000, 30000)).toBe(false);
  });

  it("allows all fast transient retries in a one-second sequence", () => {
    const attempts = [0, 250, 500, 750, 1000];
    expect(attempts.every((elapsed) => shouldRetry(elapsed, 30000))).toBe(true);
  });

  it("refuses a retry after a slow attempt has passed its retry window", () => {
    expect(shouldRetry(20000, 15000)).toBe(false);
  });
});

describe("includedSnapshotWait", () => {
  it.each([
    [{ phase_before: "banking" }, true],
    [{ phase_before: "checkpointed" }, true],
    [{ phase_before: "banked" }, false],
    [{ phase_before: "serving" }, false],
    [{ phase_before: "unknown" }, false],
    [{}, false],
    [null, false],
    [undefined, false],
  ])("returns %s for %s", (body, expected) => {
    expect(includedSnapshotWait(body)).toBe(expected);
  });
});

describe("parkedMsBreakdown", () => {
  it("returns a breakdown for valid data", () => {
    expect(parkedMsBreakdown({ parked_ms: 300, wake_ms: 550 })).toEqual({
      total: 850,
      parked: 300,
      wake: 550,
    });
  });

  it.each([
    [null, "missing body"],
    [{}, "missing parked_ms"],
    [{ parked_ms: 0, wake_ms: 10 }, "zero parked_ms"],
    [{ parked_ms: 10 }, "missing wake_ms"],
  ])("returns null for %s", (body) => {
    expect(parkedMsBreakdown(body)).toBeNull();
  });

  it("calculates total as parked plus wake", () => {
    expect(parkedMsBreakdown({ parked_ms: 125, wake_ms: 375 }).total).toBe(500);
  });
});

describe("phaseLabel", () => {
  it.each([
    ["banking", "writing snapshot"],
    ["checkpointed", "writing snapshot"],
    ["relighting", "restoring"],
    ["starting", "restoring"],
    ["cold_booting", "cold booting"],
    ["banked", "waking"],
  ])("labels %s", (state, expected) => {
    expect(phaseLabel(state)).toBe(expected);
  });

  it.each(["serving", "failed", "unknown", "", null, undefined])(
    "silences %s",
    (state) => {
      expect(phaseLabel(state)).toBe("");
    },
  );
});
