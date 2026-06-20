import { describe, it, expect } from "vitest";
import {
  interpolatePosition,
  bearingAt,
  compassArrow,
  cumulativeKm,
  evLabel,
  solarLabel,
  lightRemaining,
  formatCoord,
  photoTelemetry,
} from "./telemetry.js";

// A simple two-leg track: A->B heads due north, B->C heads due east, with a
// 10-minute gap between each fix.
const TRACK = [
  { lat: 49.0, lng: -123.0, elevation: 100, taken_at: "2025-06-21T19:00:00Z" },
  { lat: 49.1, lng: -123.0, elevation: 200, taken_at: "2025-06-21T19:10:00Z" },
  { lat: 49.1, lng: -122.8, elevation: 200, taken_at: "2025-06-21T19:20:00Z" },
];

describe("interpolatePosition", () => {
  it("returns the photo's own fix when it has one", () => {
    const photo = { lat: 1, lng: 2, elevation: 3, taken_at: TRACK[0].taken_at };
    expect(interpolatePosition(photo, TRACK)).toEqual({
      lat: 1,
      lng: 2,
      elevation: 3,
    });
  });

  it("interpolates lat/lng/elevation midway between bracketing track points", () => {
    // 19:05Z is halfway between A (19:00) and B (19:10).
    const photo = { taken_at: "2025-06-21T19:05:00Z" };
    const pos = interpolatePosition(photo, TRACK);
    expect(pos.lat).toBeCloseTo(49.05, 6);
    expect(pos.lng).toBeCloseTo(-123.0, 6);
    expect(pos.elevation).toBeCloseTo(150, 6);
  });

  it("clamps to the first point before the track and the last point after it", () => {
    const before = interpolatePosition(
      { taken_at: "2025-06-21T18:00:00Z" },
      TRACK,
    );
    expect(before.lat).toBe(49.0);
    const after = interpolatePosition(
      { taken_at: "2025-06-21T20:00:00Z" },
      TRACK,
    );
    expect(after.lat).toBe(49.1);
    expect(after.lng).toBe(-122.8);
  });
});

describe("bearingAt", () => {
  it("reads ~north for a due-north segment", () => {
    // Photo at 19:05 falls in the A->B (northbound) segment; bearing ~0deg.
    const b = bearingAt(TRACK, Date.parse("2025-06-21T19:05:00Z"));
    expect(b).toBeGreaterThanOrEqual(0);
    expect(Math.min(b, 360 - b)).toBeLessThan(1);
  });

  it("reads ~east for a due-east segment", () => {
    // Photo at 19:15 falls in the B->C (eastbound) segment; bearing ~90deg.
    const b = bearingAt(TRACK, Date.parse("2025-06-21T19:15:00Z"));
    expect(b).toBeGreaterThan(89);
    expect(b).toBeLessThan(91);
  });
});

describe("compassArrow", () => {
  it("maps cardinal bearings to arrow glyphs", () => {
    expect(compassArrow(0)).toBe("↑");
    expect(compassArrow(90)).toBe("→");
    expect(compassArrow(180)).toBe("↓");
    expect(compassArrow(270)).toBe("←");
    expect(compassArrow(null)).toBe("→");
  });
});

describe("cumulativeKm", () => {
  it("accumulates distance up to the photo and reports the day total", () => {
    // At 19:15 the A->B leg is complete (~11km) but B->C is not yet counted.
    const d = cumulativeKm(TRACK, Date.parse("2025-06-21T19:15:00Z"));
    expect(d.total).toBeGreaterThan(d.km);
    expect(d.km).toBeGreaterThan(9);
    expect(d.km).toBeLessThan(13);
    expect(d.percent).toBeGreaterThan(0);
    expect(d.percent).toBeLessThan(100);
    // The un-rounded fraction backs the elevation marker; percent is just it
    // rounded to a whole number.
    expect(d.fraction).toBeGreaterThan(0);
    expect(d.fraction).toBeLessThan(1);
    expect(Math.round(d.fraction * 100)).toBe(d.percent);
  });

  it("returns zero travelled for a photo at the day's start", () => {
    const d = cumulativeKm(TRACK, Date.parse("2025-06-21T19:00:00Z"));
    expect(d.km).toBe(0);
  });
});

describe("evLabel", () => {
  it("buckets EV into mood labels at the original thresholds", () => {
    expect(evLabel(14)).toBe("BRIGHT");
    expect(evLabel(11)).toBe("SUNNY");
    expect(evLabel(8)).toBe("OVERCAST");
    expect(evLabel(5)).toBe("DIM");
    expect(evLabel(2)).toBe("DARK");
    expect(evLabel(null)).toBe("");
  });
});

describe("solarLabel", () => {
  it("labels sun altitude bands", () => {
    expect(solarLabel(40)).toBe("DAY");
    expect(solarLabel(5)).toBe("LOW");
    expect(solarLabel(-3)).toBe("TWILIGHT");
    expect(solarLabel(-20)).toBe("NIGHT");
    expect(solarLabel(null)).toBe("--");
  });
});

describe("lightRemaining", () => {
  it("formats time until sunset and returns null once past", () => {
    const now = Date.parse("2025-06-21T19:00:00Z");
    const sunset = now + (2 * 60 + 5) * 60 * 1000; // 2h05m later
    expect(lightRemaining(now, sunset)).toBe("2h 05m");
    expect(lightRemaining(now, now - 1000)).toBeNull();
    expect(lightRemaining(now, null)).toBeNull();
  });
});

describe("formatCoord", () => {
  it("formats lat/lng with hemisphere suffixes", () => {
    expect(formatCoord(49.1234, true)).toBe("49.1234° N");
    expect(formatCoord(-12.5, true)).toBe("12.5000° S");
    expect(formatCoord(-123.9876, false)).toBe("123.9876° W");
    expect(formatCoord(8.5, false)).toBe("8.5000° E");
    expect(formatCoord(null, true)).toBe("--");
  });
});

describe("photoTelemetry", () => {
  it("bundles interpolated position plus bearing, km and solar context", () => {
    const photo = {
      taken_at: "2025-06-21T19:05:00Z",
      light_value: 11,
      iso: 100,
    };
    const t = photoTelemetry(photo, TRACK, "America/Vancouver");
    expect(t.lat).toBeCloseTo(49.05, 4);
    expect(t.evLabel).toBe("SUNNY");
    expect(t.bearingArrow).toBe("↑"); // northbound leg
    expect(t.km).toBeGreaterThanOrEqual(0);
    // Midday summer sun in BC is well above the horizon.
    expect(t.solarAltDeg).toBeGreaterThan(0);
    expect(t.solarLabel).toBe("DAY");
  });

  it("returns null for no photo", () => {
    expect(photoTelemetry(null, TRACK, "UTC")).toBeNull();
  });
});
