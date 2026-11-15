import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import TripMap from "./TripMap.svelte";
import DayMap from "./DayMap.svelte";
import RoutePreview from "./RoutePreview.svelte";

const points = [
  { lat: 49, lng: -123 },
  { lat: 50, lng: -122 },
];

describe("trip map first view", () => {
  it.each([
    ["summary", TripMap, { days: [{ dayNumber: 1, points }] }],
    ["day", DayMap, { points }],
  ])(
    "renders real %s geometry without a client fetch",
    (_, Component, props) => {
      const fetch = vi.fn(() => {
        throw new Error("unexpected first-view fetch");
      });
      vi.stubGlobal("fetch", fetch);
      try {
        const { body } = render(Component, { props });
        expect(body).toContain("<polyline");
        expect(body).toContain("Route preview");
        expect(body).toContain("2 GPS points");
        expect(body).toContain('role="img"');
        expect(body).not.toContain("Loading");
        expect(fetch).not.toHaveBeenCalled();
      } finally {
        vi.unstubAllGlobals();
      }
    },
  );

  it("renders an honest data summary for a polar route", () => {
    const { body } = render(RoutePreview, {
      props: { days: [{ points: [{ lat: 89, lng: 0 }] }] },
    });
    expect(body).toContain("polar region");
    expect(body).toContain("1 GPS point");
    expect(body).not.toContain("<polyline");
  });
});
