import { describe, it, expect } from "vitest";
import { readStarsParams, writeStarsParams } from "./urlParams.js";

function params(query) {
  return new URLSearchParams(query);
}

describe("readStarsParams", () => {
  it("defaults to live / all nights / all year with no params", () => {
    expect(readStarsParams(params(""))).toEqual({
      mode: "live",
      selectedNight: "all",
      selectedMonth: 0,
    });
  });

  it("reads a live night selection", () => {
    expect(readStarsParams(params("night=2026-06-21"))).toEqual({
      mode: "live",
      selectedNight: "2026-06-21",
      selectedMonth: 0,
    });
  });

  it("reads a historical month selection", () => {
    expect(readStarsParams(params("mode=historical&month=8"))).toEqual({
      mode: "historical",
      selectedNight: "all",
      selectedMonth: 8,
    });
  });

  it("falls back to live for an invalid mode", () => {
    expect(readStarsParams(params("mode=bogus")).mode).toBe("live");
  });

  it("ignores a malformed night and an out-of-range month", () => {
    expect(readStarsParams(params("night=soon")).selectedNight).toBe("all");
    expect(readStarsParams(params("month=13")).selectedMonth).toBe(0);
    expect(readStarsParams(params("month=0")).selectedMonth).toBe(0);
    expect(readStarsParams(params("month=-2")).selectedMonth).toBe(0);
  });
});

describe("writeStarsParams", () => {
  it("omits everything at the default (live / all / all year)", () => {
    const sp = params("");
    writeStarsParams(sp, {
      mode: "live",
      selectedNight: "all",
      selectedMonth: 0,
    });
    expect(sp.toString()).toBe("");
  });

  it("writes only night in live mode and drops a stale month", () => {
    const sp = params("month=8");
    writeStarsParams(sp, {
      mode: "live",
      selectedNight: "2026-06-21",
      selectedMonth: 8,
    });
    expect(sp.get("night")).toBe("2026-06-21");
    expect(sp.has("month")).toBe(false);
    expect(sp.has("mode")).toBe(false);
  });

  it("writes only month in historical mode and drops a stale night", () => {
    const sp = params("night=2026-06-21");
    writeStarsParams(sp, {
      mode: "historical",
      selectedNight: "2026-06-21",
      selectedMonth: 8,
    });
    expect(sp.get("mode")).toBe("historical");
    expect(sp.get("month")).toBe("8");
    expect(sp.has("night")).toBe(false);
  });

  it("round-trips a historical month", () => {
    const state = {
      mode: "historical",
      selectedNight: "all",
      selectedMonth: 3,
    };
    const sp = params("");
    writeStarsParams(sp, state);
    expect(readStarsParams(sp)).toEqual(state);
  });

  it("round-trips a live night", () => {
    const state = {
      mode: "live",
      selectedNight: "2026-07-01",
      selectedMonth: 0,
    };
    const sp = params("");
    writeStarsParams(sp, state);
    expect(readStarsParams(sp)).toEqual(state);
  });
});
