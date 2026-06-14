import { describe, it, expect } from "vitest";
import {
  monthLabel,
  monthShort,
  monthBars,
  relativeMax,
  heatWeightExpression,
} from "./heat.js";

describe("monthLabel / monthShort", () => {
  it("maps 1..12 to month names", () => {
    expect(monthLabel(1)).toBe("January");
    expect(monthLabel(12)).toBe("December");
    expect(monthShort(1)).toBe("Jan");
    expect(monthShort(6)).toBe("Jun");
  });

  it("returns empty string for out-of-range months", () => {
    expect(monthLabel(0)).toBe("");
    expect(monthLabel(13)).toBe("");
    expect(monthShort(99)).toBe("");
  });
});

describe("monthBars", () => {
  it("returns 12 bars in month order with short labels", () => {
    const bars = monthBars({});
    expect(bars).toHaveLength(12);
    expect(bars[0]).toMatchObject({ month: 1, short: "Jan" });
    expect(bars[11]).toMatchObject({ month: 12, short: "Dec" });
  });

  it("normalizes frac against the tallest month and flags it", () => {
    const bars = monthBars({ 1: 5, 6: 20, 12: 10 });
    expect(bars[0]).toMatchObject({ value: 5, frac: 0.25, isMax: false });
    expect(bars[5]).toMatchObject({ value: 20, frac: 1, isMax: true });
    expect(bars[11]).toMatchObject({ value: 10, frac: 0.5, isMax: false });
  });

  it("accepts string keys (JSON object keys stringify)", () => {
    const bars = monthBars({ 3: 8, 9: 16 });
    expect(bars[2]).toMatchObject({ value: 8, frac: 0.5 });
    expect(bars[8]).toMatchObject({ value: 16, frac: 1, isMax: true });
  });

  it("yields all-zero bars (frac 0, no max) for an empty or missing map", () => {
    for (const bars of [monthBars({}), monthBars(null), monthBars(undefined)]) {
      expect(bars.every((b) => b.value === 0 && b.frac === 0 && !b.isMax)).toBe(
        true,
      );
    }
  });

  it("coerces negative or non-finite counts to zero", () => {
    const bars = monthBars({ 1: -4, 2: NaN, 3: "x", 4: 6 });
    expect(bars[0].value).toBe(0);
    expect(bars[1].value).toBe(0);
    expect(bars[2].value).toBe(0);
    expect(bars[3]).toMatchObject({ value: 6, frac: 1, isMax: true });
  });
});

describe("relativeMax", () => {
  it("returns the largest finite value", () => {
    expect(relativeMax([3, 1, 42, 7])).toBe(42);
  });

  it("never drops below the floor (default 1) so the domain stays ascending", () => {
    expect(relativeMax([])).toBe(1);
    expect(relativeMax([0, 0, 0])).toBe(1);
    expect(relativeMax([0.2, 0.5])).toBe(1);
  });

  it("honours a custom floor", () => {
    expect(relativeMax([2, 3], 10)).toBe(10);
    expect(relativeMax([20, 3], 10)).toBe(20);
  });

  it("ignores non-finite values", () => {
    expect(relativeMax([NaN, Infinity, 5, null, "x"])).toBe(5);
  });
});

describe("heatWeightExpression", () => {
  it("builds an interpolate from 0..max onto 0..1", () => {
    expect(heatWeightExpression(80)).toEqual([
      "interpolate",
      ["linear"],
      ["get", "heat"],
      0,
      0,
      80,
      1,
    ]);
  });
});
