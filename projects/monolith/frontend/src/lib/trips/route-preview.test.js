import { describe, expect, it } from "vitest";
import { routePreview } from "./route-preview.js";

const point = (lng, lat) => ({ lng, lat });

describe("route preview geometry", () => {
  it("projects north upward with Mercator latitude spacing", () => {
    const preview = routePreview([
      { points: [point(0, 0), point(0, 30), point(0, 60)] },
    ]);
    const [south, middle, north] = preview.routes[0].samples;
    expect(south[0]).toBe(middle[0]);
    expect(middle[0]).toBe(north[0]);
    expect(south[1]).toBeGreaterThan(middle[1]);
    expect(middle[1]).toBeGreaterThan(north[1]);
    expect((south[1] - middle[1]) / (south[1] - north[1])).toBeCloseTo(
      0.417,
      2,
    );
    expect(preview.pointCount).toBe(3);
  });

  it("unwraps date-line crossings across day boundaries", () => {
    const preview = routePreview([
      { points: [point(178, 50), point(179, 50)] },
      { points: [point(-179, 50), point(-178, 50)] },
    ]);
    const [a, b] = preview.routes[0].samples;
    const [c, d] = preview.routes[1].samples;
    expect(a[0]).toBeLessThan(b[0]);
    expect(b[0]).toBeLessThan(c[0]);
    expect(c[0]).toBeLessThan(d[0]);
    expect(c[0] - b[0]).toBeCloseTo(2 * (b[0] - a[0]), 1);
    expect(preview.reason).toBeNull();
  });

  it("bounds markup while retaining endpoints and complete point counts", () => {
    const points = Array.from({ length: 2000 }, (_, i) =>
      point(-130 + i / 1000, 50 + i / 10000),
    );
    const full = routePreview([{ points }]);
    const endpoints = routePreview([{ points: [points[0], points.at(-1)] }]);
    expect(full.routes[0].samples).toHaveLength(128);
    expect(full.routes[0].samples[0]).toEqual(endpoints.routes[0].samples[0]);
    expect(full.routes[0].samples.at(-1)).toEqual(
      endpoints.routes[0].samples.at(-1),
    );
    expect(full.pointCount).toBe(2000);
  });

  it("centres a single point and handles missing or invalid coordinates", () => {
    const preview = routePreview([
      { points: [point(null, 50), point(181, 50), point(-4, 56)] },
    ]);
    expect(preview.routes[0].samples).toEqual([[320, 160]]);
    expect(preview.pointCount).toBe(1);
    expect(routePreview([])).toMatchObject({
      routes: [],
      pointCount: 0,
      reason: "empty",
    });
  });

  it("uses a data summary for polar or very long journeys", () => {
    expect(routePreview([{ points: [point(0, 89)] }])).toMatchObject({
      pointCount: 1,
      routes: [],
      reason: "polar",
    });
    expect(
      routePreview(
        Array.from({ length: 33 }, () => ({ points: [point(-4, 56)] })),
      ),
    ).toMatchObject({
      dayCount: 33,
      pointCount: 33,
      routes: [],
      reason: "extent",
    });
    expect(
      routePreview([
        { points: [0, 120, -120, 0, 120].map((lon) => point(lon, 0)) },
      ]),
    ).toMatchObject({ routes: [], reason: "extent" });
  });
});
