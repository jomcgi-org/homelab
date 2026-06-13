import { describe, it, expect } from "vitest";
import {
  monthLabel,
  monthShort,
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
