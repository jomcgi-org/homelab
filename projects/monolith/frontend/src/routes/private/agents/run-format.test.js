import { describe, expect, test } from "vitest";
import { fmtCost, fmtDur, ordinal, relSeconds } from "./run-format.js";

describe("run formatting", () => {
  test("formats durations using the contract units", () => {
    expect(fmtDur(12)).toBe("12s");
    expect(fmtDur(90)).toBe("1m");
    expect(fmtDur(3660)).toBe("1h 1m");
    expect(fmtDur(172800)).toBe("2d");
  });
  test("formats costs and ordinals", () => {
    expect(fmtCost(0.004)).toBe("$0.0040");
    expect(fmtCost(2)).toBe("$2.00");
    expect(ordinal(3)).toBe("3rd");
    expect(ordinal(8)).toBe("8th");
  });
  test("calculates relative seconds", () =>
    expect(relSeconds("2026-08-10T00:00:00Z", "2026-08-10T00:01:02Z")).toBe(
      62,
    ));
});
