import { describe, it, expect } from "vitest";
import { periodForHour } from "./period.js";

describe("periodForHour", () => {
  it("returns 'night' for hour 0", () => {
    expect(periodForHour(0)).toBe("night");
  });

  it("returns 'night' for hour 4", () => {
    expect(periodForHour(4)).toBe("night");
  });

  it("returns 'dawn' for hour 5", () => {
    expect(periodForHour(5)).toBe("dawn");
  });

  it("returns 'dawn' for hour 10", () => {
    expect(periodForHour(10)).toBe("dawn");
  });

  it("returns 'day' for hour 11", () => {
    expect(periodForHour(11)).toBe("day");
  });

  it("returns 'day' for hour 16", () => {
    expect(periodForHour(16)).toBe("day");
  });

  it("returns 'dusk' for hour 17", () => {
    expect(periodForHour(17)).toBe("dusk");
  });

  it("returns 'dusk' for hour 21", () => {
    expect(periodForHour(21)).toBe("dusk");
  });

  it("returns 'night' for hour 22", () => {
    expect(periodForHour(22)).toBe("night");
  });

  it("returns 'night' for hour 23", () => {
    expect(periodForHour(23)).toBe("night");
  });
});
