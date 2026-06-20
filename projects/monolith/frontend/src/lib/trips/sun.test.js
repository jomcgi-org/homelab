import { describe, it, expect } from "vitest";
import { sunAltitude, sunsetTime } from "./sun.js";

const toDeg = (rad) => (rad * 180) / Math.PI;

// Mid-latitude site in southern BC (lat 49, lng -123). At solar noon the sun's
// altitude is roughly 90 - lat +/- the solar declination (+23.44deg at the June
// solstice, -23.44deg at December), so these are physical sanity checks rather
// than golden values copied from the suncalc package.
describe("sunAltitude", () => {
  it("is high near the summer-solstice solar noon", () => {
    // 2025-06-21 19:12Z is ~12:12 PDT (UTC-7), close to local solar noon.
    const alt = toDeg(sunAltitude(new Date("2025-06-21T19:12:00Z"), 49, -123));
    expect(alt).toBeGreaterThan(55);
    expect(alt).toBeLessThan(70);
  });

  it("is low near the winter-solstice solar noon", () => {
    const alt = toDeg(sunAltitude(new Date("2025-12-21T20:12:00Z"), 49, -123));
    expect(alt).toBeGreaterThan(10);
    expect(alt).toBeLessThan(25);
  });

  it("is below the horizon at local midnight", () => {
    // 2025-06-22T08:00Z is ~01:00 PDT.
    const alt = toDeg(sunAltitude(new Date("2025-06-22T08:00:00Z"), 49, -123));
    expect(alt).toBeLessThan(0);
  });
});

describe("sunsetTime", () => {
  it("returns a sunset after local noon at a mid-latitude site", () => {
    const noon = new Date("2025-06-21T19:12:00Z");
    const set = sunsetTime(noon, 49, -123);
    expect(set).toBeInstanceOf(Date);
    expect(set.getTime()).toBeGreaterThan(noon.getTime());
  });

  it("returns null in the polar day (sun never sets)", () => {
    // 2025-06-21 at 80N: continuous daylight, no sunset.
    expect(sunsetTime(new Date("2025-06-21T12:00:00Z"), 80, 0)).toBeNull();
  });
});
