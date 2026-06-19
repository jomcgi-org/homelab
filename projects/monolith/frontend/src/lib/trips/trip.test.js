import { describe, it, expect } from "vitest";
import {
  dayKey,
  groupByDay,
  deriveTripStats,
  dayLabel,
  dayPhotos,
  routeDistanceKm,
} from "./trip.js";

// Two days in America/Vancouver (UTC-8 in winter): the 23:30 local point of
// 2025-01-01 is 2025-01-02T07:30Z, so day grouping must use the trip zone, not
// the (UTC) server clock.
const POINTS = [
  {
    id: "a",
    lat: 49.0,
    lng: -123.0,
    taken_at: "2025-01-01T17:00:00Z",
    image: "a.jpg",
    elevation: 10,
  },
  {
    id: "b",
    lat: 49.5,
    lng: -123.5,
    taken_at: "2025-01-02T01:00:00Z",
    image: null,
    elevation: 120,
  },
  {
    id: "c",
    lat: 50.0,
    lng: -124.0,
    taken_at: "2025-01-02T20:00:00Z",
    image: "c.jpg",
    elevation: 80,
  },
];

describe("dayKey", () => {
  it("buckets by the trip timezone, not UTC", () => {
    // 2025-01-02T01:00Z is still Jan 1 in Vancouver.
    expect(dayKey("2025-01-02T01:00:00Z", "America/Vancouver")).toBe(
      "2025-01-01",
    );
    expect(dayKey("2025-01-02T20:00:00Z", "America/Vancouver")).toBe(
      "2025-01-02",
    );
  });
});

describe("groupByDay", () => {
  it("groups chronologically and numbers days from 1", () => {
    const days = groupByDay(POINTS, "America/Vancouver");
    expect(days.map((d) => d.dayNumber)).toEqual([1, 2]);
    expect(days[0].points).toHaveLength(2); // a + b are both Jan 1 local
    expect(days[1].points).toHaveLength(1);
  });
});

describe("deriveTripStats", () => {
  it("returns null without points and totals with them", () => {
    expect(deriveTripStats([], "UTC")).toBeNull();
    const stats = deriveTripStats(POINTS, "America/Vancouver", {
      coldest_temp: -34,
    });
    expect(stats.totalDays).toBe(2);
    expect(stats.totalPoints).toBe(3);
    expect(stats.coldestTemp).toBe(-34);
    expect(stats.maxLat).toBe(50.0);
    expect(stats.hasElevation).toBe(true);
    expect(stats.totalDistance).toBe(routeDistanceKm(POINTS));
  });
});

describe("dayLabel + dayPhotos", () => {
  it("reads labels from the days blob and falls back", () => {
    expect(dayLabel({ 2: { label: "B TO C" } }, 2)).toBe("B TO C");
    expect(dayLabel({}, 3)).toBe("Day 3");
  });
  it("dayPhotos keeps only image-bearing points", () => {
    expect(dayPhotos(POINTS).map((p) => p.id)).toEqual(["a", "c"]);
  });
});
