import { describe, it, expect } from "vitest";
import { readShipsParams, writeShipsParams } from "./urlParams.js";

const ALL_KEYS = ["passenger", "cargo", "tanker", "hsc", "special", "unknown"];

function params(query) {
  return new URLSearchParams(query);
}

describe("readShipsParams", () => {
  it("defaults to all types on / vessels mode / no selection", () => {
    const got = readShipsParams(params(""), ALL_KEYS);
    expect([...got.active].sort()).toEqual([...ALL_KEYS].sort());
    expect(got.mode).toBe("vessels");
    expect(got.mmsi).toBeNull();
  });

  it("reads a subset of types", () => {
    const got = readShipsParams(params("types=cargo,tanker"), ALL_KEYS);
    expect([...got.active].sort()).toEqual(["cargo", "tanker"]);
  });

  it("drops unknown type tokens", () => {
    const got = readShipsParams(params("types=cargo,submarine"), ALL_KEYS);
    expect([...got.active]).toEqual(["cargo"]);
  });

  it("falls back to all-on when types resolves to empty (never hide all)", () => {
    const got = readShipsParams(params("types=submarine,zeppelin"), ALL_KEYS);
    expect([...got.active].sort()).toEqual([...ALL_KEYS].sort());
  });

  it("reads heat mode and a vessel selection", () => {
    const got = readShipsParams(params("mode=heat&mmsi=232003456"), ALL_KEYS);
    expect(got.mode).toBe("heat");
    expect(got.mmsi).toBe("232003456");
  });

  it("falls back to vessels for an invalid mode and ignores a non-numeric mmsi", () => {
    const got = readShipsParams(params("mode=globe&mmsi=abc"), ALL_KEYS);
    expect(got.mode).toBe("vessels");
    expect(got.mmsi).toBeNull();
  });
});

describe("writeShipsParams", () => {
  it("omits everything at the default (all types, vessels, no selection)", () => {
    const sp = params("");
    writeShipsParams(
      sp,
      { active: new Set(ALL_KEYS), mode: "vessels", mmsi: null },
      ALL_KEYS,
    );
    expect(sp.toString()).toBe("");
  });

  it("writes a type subset in legend order", () => {
    const sp = params("");
    writeShipsParams(
      sp,
      { active: new Set(["tanker", "cargo"]), mode: "vessels", mmsi: null },
      ALL_KEYS,
    );
    // Legend order: cargo before tanker.
    expect(sp.get("types")).toBe("cargo,tanker");
  });

  it("writes mode and mmsi when set", () => {
    const sp = params("");
    writeShipsParams(
      sp,
      { active: new Set(ALL_KEYS), mode: "heat", mmsi: "232003456" },
      ALL_KEYS,
    );
    expect(sp.has("types")).toBe(false);
    expect(sp.get("mode")).toBe("heat");
    expect(sp.get("mmsi")).toBe("232003456");
  });

  it("round-trips a type subset + selection", () => {
    const sp = params("");
    writeShipsParams(
      sp,
      { active: new Set(["passenger", "unknown"]), mode: "vessels", mmsi: "1" },
      ALL_KEYS,
    );
    const got = readShipsParams(sp, ALL_KEYS);
    expect([...got.active].sort()).toEqual(["passenger", "unknown"]);
    expect(got.mode).toBe("vessels");
    expect(got.mmsi).toBe("1");
  });
});
