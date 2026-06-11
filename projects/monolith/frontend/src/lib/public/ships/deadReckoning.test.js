import { describe, it, expect } from "vitest";
import { deadReckon } from "./deadReckoning.js";

// One nautical mile per minute at 60 knots; at 10 knots over 60s a vessel
// covers 10 kn * 0.514444 m/s/kn * 60 s ~= 308.7 m.
const EXPECTED_M = 10 * 0.514444 * 60;

// Meters per degree of longitude at the equator (cos(0) = 1).
const M_PER_DEG_LON = (6371000 * Math.PI) / 180;

describe("deadReckon", () => {
  it("moves ~309 m due east on heading 090 at 10 kn for 60 s", () => {
    const start = { lat: 0, lon: 0, speed: 10, course: 90 };
    const next = deadReckon(start, 60);

    // Latitude is essentially unchanged on a due-east heading.
    expect(Math.abs(next.lat)).toBeLessThan(1e-6);
    // Longitude increases (moving east).
    expect(next.lon).toBeGreaterThan(0);

    const movedM = next.lon * M_PER_DEG_LON;
    expect(movedM).toBeGreaterThan(305);
    expect(movedM).toBeLessThan(312);
    expect(Math.abs(movedM - EXPECTED_M)).toBeLessThan(2);
  });

  it("returns the same point when speed is 0", () => {
    const start = { lat: 51.5, lon: -0.12, speed: 0, course: 180 };
    expect(deadReckon(start, 120)).toEqual({ lat: 51.5, lon: -0.12 });
  });

  it("returns the same point when speed is missing", () => {
    const start = { lat: 51.5, lon: -0.12, course: 180 };
    expect(deadReckon(start, 120)).toEqual({ lat: 51.5, lon: -0.12 });
  });

  it("treats an undefined course as 0 (due north)", () => {
    const start = { lat: 0, lon: 0, speed: 10 };
    const next = deadReckon(start, 60);

    // Course defaults to 0 deg = north: latitude increases, longitude steady.
    expect(next.lat).toBeGreaterThan(0);
    expect(Math.abs(next.lon)).toBeLessThan(1e-6);

    const movedM = (next.lat * Math.PI * 6371000) / 180;
    expect(Math.abs(movedM - EXPECTED_M)).toBeLessThan(2);
  });
});
