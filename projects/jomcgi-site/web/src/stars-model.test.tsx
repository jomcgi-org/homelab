import { describe, expect, it } from "vitest";
import { cells } from "./stars-geometry";
import {
  dataPath,
  historyView,
  liveHeat,
  liveWindows,
  monthBars,
  nightKey,
  readStarsParams,
  shareForecastHours,
  starsNights,
  writeStarsParams,
  type Site,
} from "./stars-model";
import {
  isStale,
  makeSummary,
  summaryHtml,
} from "../../data/stars-summary.mjs";

const now = Date.parse("2026-11-17T22:30:00Z");
const site: Site = {
  id: "a",
  name: "Galloway",
  lat: 55,
  lon: -4,
  best_hours: [
    { time: "2026-11-17T21:00:00Z", cloud_area_fraction: 2 },
    { time: "2026-11-17T22:00:00Z", cloud_area_fraction: 3 },
    { time: "2026-11-18T01:00:00Z", cloud_area_fraction: 4, dark: true },
    { time: "2026-11-18T02:00:00Z", cloud_area_fraction: 5, dark: false },
    { time: "2026-11-18T23:00:00Z", cloud_area_fraction: 6 },
  ],
};
describe("stars forecast and history port", () => {
  it("marks a stopped site refresh stale at 90 minutes while a three-hour forecast stays fresh", () => {
    const data = { fetched_at: "2026-11-17T20:00:00Z", sites: [site] };
    const summary = makeSummary(data, now, "2026-11-17T21:00:00Z");
    expect(summaryHtml(summary, now - 1)).not.toContain("Stale");
    const stale = summaryHtml(summary, now);
    expect(stale).toContain("Stale site snapshot");
    expect(stale).toContain("Site data checked at");
    expect(stale).not.toContain("Stale —");
    expect(stale).not.toContain("unavailable");
    expect(stale).toContain("clear dark hours");
    // A successful site refresh can publish the same upstream forecast.
    expect(
      summaryHtml(makeSummary(data, now, new Date(now).toISOString()), now),
    ).not.toContain("Stale");
  });
  it("shares identical complete hours without deleting fields, sites or records", () => {
    const hour = { ...site.best_hours![0]!, futureField: { nested: "keep" } };
    const input = {
      sites: [
        { ...site, best_hours: [hour] },
        { ...site, id: "b", best_hours: [{ ...hour }] },
      ],
      futureField: 9,
    };
    const result = shareForecastHours(input);
    expect(result).toEqual(input);
    expect(result.sites[0]!.best_hours![0]).toBe(
      result.sites[1]!.best_hours![0],
    );
    expect(input.sites[0]!.best_hours[0]).not.toBe(
      input.sites[1]!.best_hours[0],
    );
  });
  it("keeps the in-progress hour and folds pre-dawn hours into the UK viewing night", () => {
    expect(liveWindows(site, now)).toHaveLength(4);
    expect(nightKey("2026-11-18T01:00:00Z")).toBe("2026-11-17");
    expect(nightKey("2026-07-18T01:00:00+01:00")).toBe("2026-07-17");
    expect(starsNights([site], "astronomical", now)).toEqual([
      "2026-11-17",
      "2026-11-18",
    ]);
    expect(starsNights([site], "astronomical", now + 48 * 3_600_000)).toEqual(
      [],
    );
  });
  it("scores the best selected night and includes twilight only in the summer fallback", () => {
    const nights = new Set(["2026-11-17", "2026-11-18"]);
    expect(liveHeat(site, "astronomical", nights, now)).toBe(2);
    expect(liveHeat(site, "twilight", nights, now)).toBe(3);
    expect(liveHeat(site, "astronomical", new Set(["2026-11-18"]), now)).toBe(
      1,
    );
    expect(liveHeat(site, "astronomical", new Set(["2026-11-19"]), now)).toBe(
      0,
    );
  });
  it("projects all-year/month totals, drops no-dark rows, preserves all 12 chart values", () => {
    const rows = [
      {
        ...site,
        clear: [2, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 6],
        dark: [10, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 30],
      },
    ];
    expect(historyView(rows, 0)[0]).toMatchObject({
      clear_dark_hours: 12,
      dark_hours: 60,
      clear_rate: 0.2,
      clear: rows[0]!.clear,
    });
    expect(historyView(rows, 2)[0]!.clear_dark_hours).toBe(4);
    expect(historyView(rows, 3)).toEqual([]);
    const bars = monthBars(rows[0]!.clear);
    expect(bars).toHaveLength(12);
    expect(bars[11]).toMatchObject({
      month: "December",
      value: 6,
      frac: 1,
      isMax: true,
    });
    expect(monthBars([NaN, -1]).every((bar) => bar.value === 0)).toBe(true);
  });
  it("uses GeoJSON objects for both layers and normalizes the visible cells", () => {
    for (const mode of ["live", "historical"] as const) {
      const data = cells({
        sites: [{ ...site, clear_dark_hours: 30 }],
        mode,
        darkness: "astronomical",
        activeNights: new Set(["2026-11-17"]),
        now,
      });
      expect(data).toMatchObject({
        type: "FeatureCollection",
        features: [
          {
            properties: { id: "a", score: 100 },
            geometry: { type: "Polygon" },
          },
        ],
      });
    }
  });
  it("round-trips view URLs, removes inactive filters and preserves unrelated parameters", () => {
    const params = new URLSearchParams(
      "ref=x&mode=historical&month=12&night=2026-11-17",
    );
    const value = readStarsParams(params);
    expect(writeStarsParams(params, value).toString()).toBe(
      "ref=x&mode=historical&month=12",
    );
    expect(
      writeStarsParams(params, { ...value, mode: "live" }).toString(),
    ).toBe("ref=x&night=2026-11-17");
    expect(
      readStarsParams(new URLSearchParams("mode=bad&month=13&night=bad")),
    ).toEqual({ mode: "live", selectedMonth: 0, selectedNight: "all" });
  });
  it("permits only this origin’s immutable stars data files", () => {
    expect(dataPath("/data/stars/v/historyMap-abcdef123456.json")).toContain(
      "historyMap",
    );
    for (const path of [
      "https://hub.jomcgi.dev/api/stars",
      "//example.org/a",
      "data:abc",
      "/data/hikes/v/sites-abcdef123456.json",
      "/data/stars/v/../../a",
    ])
      expect(() => dataPath(path)).toThrow();
  });
  it("marks forecasts stale at nine hours, not during a normal three-hour cycle without discarding the summary", () => {
    const asOf = "2026-11-17T13:30:00Z";
    expect(isStale(asOf, Date.parse(asOf) + 90 * 60_000)).toBe(false);
    expect(isStale(asOf, Date.parse(asOf) + 3 * 60 * 60_000)).toBe(false);
    expect(isStale(asOf, now - 1)).toBe(false);
    expect(isStale(asOf, now)).toBe(true);
    const summary = makeSummary(
      {
        fetched_at: asOf,
        sites: [
          {
            ...site,
            name: "</script><img src=x onerror=alert(1)>",
            clear_dark_hours: 5,
          },
        ],
      },
      now,
    );
    const html = summaryHtml(summary);
    expect(html).toContain("Stale");
    expect(html).toContain("<time");
    expect(html).toContain("5 clear dark hours");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("unavailable");
  });
});
