import { describe, it, expect } from "vitest";
import {
  calculateDistance,
  filterWalksByCharacteristics,
  filterWalksByLocation,
  groupWindowsByDay,
  upcomingUkDays,
  viableInNextDays,
} from "./filters.js";

// Unix seconds for a given UTC wall-clock time, the format the windows use.
function utc(iso) {
  return Math.floor(Date.parse(iso) / 1000);
}

function walk(overrides = {}) {
  return {
    uuid: "w",
    name: "Test walk",
    url: "https://example.com",
    distance_km: 10,
    ascent_m: 500,
    duration_h: 4,
    summary: "",
    latitude: 57.0,
    longitude: -4.0,
    windows: [],
    ...overrides,
  };
}

describe("calculateDistance", () => {
  it("computes a known haversine distance", () => {
    // Edinburgh to Glasgow is roughly 67 km great-circle.
    const d = calculateDistance(55.9533, -3.1883, 55.8642, -4.2518);
    expect(d).toBeGreaterThan(60);
    expect(d).toBeLessThan(72);
  });

  it("is zero for identical points", () => {
    expect(calculateDistance(57, -4, 57, -4)).toBeCloseTo(0, 6);
  });
});

describe("filterWalksByCharacteristics", () => {
  const walks = [
    walk({ uuid: "a", duration_h: 2, distance_km: 5, ascent_m: 200 }),
    walk({ uuid: "b", duration_h: 6, distance_km: 20, ascent_m: 1000 }),
  ];

  it("keeps everything when no bounds are given", () => {
    expect(filterWalksByCharacteristics(walks, {}).map((w) => w.uuid)).toEqual([
      "a",
      "b",
    ]);
  });

  it("treats duration bounds as inclusive", () => {
    // minDuration exactly equal to a walk's duration keeps it (inclusive lower).
    const kept = filterWalksByCharacteristics(walks, { minDuration: 6 });
    expect(kept.map((w) => w.uuid)).toEqual(["b"]);
    // maxDuration exactly equal keeps it (inclusive upper).
    const kept2 = filterWalksByCharacteristics(walks, { maxDuration: 2 });
    expect(kept2.map((w) => w.uuid)).toEqual(["a"]);
  });

  it("treats distance bounds as inclusive", () => {
    const kept = filterWalksByCharacteristics(walks, {
      minDistance: 5,
      maxDistance: 5,
    });
    expect(kept.map((w) => w.uuid)).toEqual(["a"]);
  });

  it("treats maxAscent as inclusive and drops above it", () => {
    // Exactly at the cap is kept; strictly above is dropped.
    expect(
      filterWalksByCharacteristics(walks, { maxAscent: 200 }).map(
        (w) => w.uuid,
      ),
    ).toEqual(["a"]);
    expect(
      filterWalksByCharacteristics(walks, { maxAscent: 199 }).map(
        (w) => w.uuid,
      ),
    ).toEqual([]);
  });
});

describe("filterWalksByLocation", () => {
  it("keeps walks within the radius and sorts nearest-first", () => {
    const near = walk({ uuid: "near", latitude: 55.96, longitude: -3.19 });
    const far = walk({ uuid: "far", latitude: 57.5, longitude: -5.0 });
    const out = filterWalksByLocation([far, near], 55.95, -3.19, 50);
    expect(out.map((w) => w.uuid)).toEqual(["near"]);
    expect(out[0].distance_from_user).toBeLessThan(5);
  });

  it("sorts multiple in-range walks by distance", () => {
    const a = walk({ uuid: "a", latitude: 55.96, longitude: -3.19 }); // ~1 km
    const b = walk({ uuid: "b", latitude: 56.2, longitude: -3.19 }); // ~28 km
    const out = filterWalksByLocation([b, a], 55.95, -3.19, 100);
    expect(out.map((w) => w.uuid)).toEqual(["a", "b"]);
    expect(out[0].distance_from_user).toBeLessThan(out[1].distance_from_user);
  });
});

describe("groupWindowsByDay", () => {
  it("buckets windows by UK-local calendar day", () => {
    const now = new Date("2026-06-15T00:00:00Z");
    const w = walk({
      windows: [
        // BST (UTC+1): 12:00Z is 13:00 UK on the 15th.
        [utc("2026-06-15T12:00:00Z"), 14, 0, 10, 30],
        // 22:30Z is 23:30 UK, still the 15th.
        [utc("2026-06-15T22:30:00Z"), 12, 0, 8, 40],
        // 23:30Z is 00:30 UK on the 16th, so a different bucket.
        [utc("2026-06-16T08:00:00Z"), 13, 0, 9, 20],
      ],
    });
    const byDay = groupWindowsByDay(w, now);
    expect(Object.keys(byDay).sort()).toEqual(["2026-06-15", "2026-06-16"]);
    expect(byDay["2026-06-15"]).toHaveLength(2);
    expect(byDay["2026-06-16"]).toHaveLength(1);
  });

  it("drops windows that have already started relative to now", () => {
    const now = new Date("2026-06-15T15:00:00Z");
    const w = walk({
      windows: [
        [utc("2026-06-15T10:00:00Z"), 14, 0, 10, 30], // past
        [utc("2026-06-15T18:00:00Z"), 14, 0, 10, 30], // future
      ],
    });
    const byDay = groupWindowsByDay(w, now);
    expect(byDay["2026-06-15"]).toHaveLength(1);
  });
});

describe("upcomingUkDays", () => {
  it("returns n consecutive UK day strings starting today", () => {
    const now = new Date("2026-06-15T09:00:00Z");
    expect(upcomingUkDays(3, now)).toEqual([
      "2026-06-15",
      "2026-06-16",
      "2026-06-17",
    ]);
  });

  it("yields consecutive days across the spring-forward DST boundary", () => {
    // UK clocks go forward on 2026-03-29 (01:00 GMT -> 02:00 BST), so that day
    // is only 23 hours long. Anchoring on the UK calendar day (rather than
    // adding 86_400_000 ms) must still yield consecutive distinct days with no
    // skip or duplicate. Start just before UK midnight on the spring-forward
    // day: 2026-03-28T23:30Z is 23:30 GMT on the 28th.
    const now = new Date("2026-03-28T23:30:00Z");
    expect(upcomingUkDays(4, now)).toEqual([
      "2026-03-28",
      "2026-03-29",
      "2026-03-30",
      "2026-03-31",
    ]);
  });
});

describe("viableInNextDays", () => {
  const now = new Date("2026-06-15T06:00:00Z");

  it("is true when a future window falls inside the horizon", () => {
    const w = walk({
      windows: [[utc("2026-06-16T12:00:00Z"), 14, 0, 10, 30]],
    });
    expect(viableInNextDays(w, 2, now)).toBe(true);
  });

  it("is false when the only window is beyond the horizon", () => {
    const w = walk({
      windows: [[utc("2026-06-20T12:00:00Z"), 14, 0, 10, 30]],
    });
    expect(viableInNextDays(w, 2, now)).toBe(false);
  });

  it("is false for a walk with no windows", () => {
    expect(viableInNextDays(walk(), 7, now)).toBe(false);
  });

  it("counts a window today for n=1", () => {
    const w = walk({
      windows: [[utc("2026-06-15T18:00:00Z"), 14, 0, 10, 30]],
    });
    expect(viableInNextDays(w, 1, now)).toBe(true);
  });
});
