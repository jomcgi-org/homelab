import { describe, it, expect } from "vitest";
import {
  monthLabel,
  monthShort,
  monthBars,
  projectHistory,
  historyView,
  relativeMax,
  heatWeightExpression,
  isUpcoming,
  nightKey,
  liveWindows,
  starsNights,
} from "./heat.js";

// A full 12-element month array with the given {monthNumber: value} entries set
// and the rest zero, mirroring the /history payload's `clear`/`dark` shape.
function months(entries) {
  const arr = Array(12).fill(0);
  for (const [m, v] of Object.entries(entries)) arr[Number(m) - 1] = v;
  return arr;
}

describe("monthLabel / monthShort", () => {
  it("maps 1..12 to month names", () => {
    expect(monthLabel(1)).toBe("January");
    expect(monthLabel(12)).toBe("December");
    expect(monthShort(1)).toBe("Jan");
    expect(monthShort(6)).toBe("Jun");
  });

  it("returns empty string for out-of-range months", () => {
    expect(monthLabel(0)).toBe("");
    expect(monthLabel(13)).toBe("");
    expect(monthShort(99)).toBe("");
  });
});

describe("monthBars", () => {
  it("returns 12 bars in month order with short labels", () => {
    const bars = monthBars({});
    expect(bars).toHaveLength(12);
    expect(bars[0]).toMatchObject({ month: 1, short: "Jan" });
    expect(bars[11]).toMatchObject({ month: 12, short: "Dec" });
  });

  it("normalizes frac against the tallest month and flags it", () => {
    const bars = monthBars({ 1: 5, 6: 20, 12: 10 });
    expect(bars[0]).toMatchObject({ value: 5, frac: 0.25, isMax: false });
    expect(bars[5]).toMatchObject({ value: 20, frac: 1, isMax: true });
    expect(bars[11]).toMatchObject({ value: 10, frac: 0.5, isMax: false });
  });

  it("accepts string keys (JSON object keys stringify)", () => {
    const bars = monthBars({ 3: 8, 9: 16 });
    expect(bars[2]).toMatchObject({ value: 8, frac: 0.5 });
    expect(bars[8]).toMatchObject({ value: 16, frac: 1, isMax: true });
  });

  it("yields all-zero bars (frac 0, no max) for an empty or missing map", () => {
    for (const bars of [monthBars({}), monthBars(null), monthBars(undefined)]) {
      expect(bars.every((b) => b.value === 0 && b.frac === 0 && !b.isMax)).toBe(
        true,
      );
    }
  });

  it("coerces negative or non-finite counts to zero", () => {
    const bars = monthBars({ 1: -4, 2: NaN, 3: "x", 4: 6 });
    expect(bars[0].value).toBe(0);
    expect(bars[1].value).toBe(0);
    expect(bars[2].value).toBe(0);
    expect(bars[3]).toMatchObject({ value: 6, frac: 1, isMax: true });
  });

  it("accepts a 12-element array (index 0 = January), the /history shape", () => {
    const bars = monthBars(months({ 1: 5, 6: 20, 12: 10 }));
    expect(bars[0]).toMatchObject({ value: 5, frac: 0.25, isMax: false });
    expect(bars[5]).toMatchObject({ value: 20, frac: 1, isMax: true });
    expect(bars[11]).toMatchObject({ value: 10, frac: 0.5, isMax: false });
  });
});

describe("projectHistory", () => {
  const site = {
    id: "galloway-forest",
    name: "Galloway Forest Park",
    lat: 55.083,
    lon: -4.4,
    clear: months({ 1: 8, 12: 25 }),
    dark: months({ 1: 30, 12: 50 }),
  };

  it("projects a single month (1-indexed) to scalar headline counts", () => {
    const row = projectHistory(site, 12);
    expect(row).toMatchObject({
      id: "galloway-forest",
      name: "Galloway Forest Park",
      lat: 55.083,
      clear_dark_hours: 25,
      dark_hours: 50,
      clear_rate: 0.5,
    });
    // The per-site array rides along so the card chart needs no extra request.
    expect(row.clear).toBe(site.clear);
  });

  it("sums every month for the all-year view (month 0)", () => {
    const row = projectHistory(site, 0);
    expect(row.clear_dark_hours).toBe(8 + 25);
    expect(row.dark_hours).toBe(30 + 50);
    expect(row.clear_rate).toBeCloseTo(33 / 80);
  });

  it("returns null when the selected month has no dark hours (drops the site)", () => {
    expect(projectHistory(site, 6)).toBe(null); // June: zero dark
    const allZero = { ...site, clear: months({}), dark: months({}) };
    expect(projectHistory(allZero, 0)).toBe(null);
    expect(projectHistory(null, 0)).toBe(null);
  });
});

describe("historyView", () => {
  const sites = [
    {
      id: "tomintoul",
      clear: months({ 6: 9 }),
      dark: months({ 6: 10 }),
    },
    {
      id: "galloway-forest",
      clear: months({ 1: 8, 12: 25 }),
      dark: months({ 1: 30, 12: 50 }),
    },
  ];

  it("projects, drops zero-dark sites, and sorts by clear-dark hours desc", () => {
    // December: only galloway has dark hours.
    const dec = historyView(sites, 12);
    expect(dec.map((s) => s.id)).toEqual(["galloway-forest"]);
    expect(dec[0].clear_dark_hours).toBe(25);

    // All year: galloway (33) leads tomintoul (9).
    const year = historyView(sites, 0);
    expect(year.map((s) => s.id)).toEqual(["galloway-forest", "tomintoul"]);
  });

  it("tolerates a missing site list", () => {
    expect(historyView(null, 0)).toEqual([]);
    expect(historyView(undefined, 6)).toEqual([]);
  });
});

describe("relativeMax", () => {
  it("returns the largest finite value", () => {
    expect(relativeMax([3, 1, 42, 7])).toBe(42);
  });

  it("never drops below the floor (default 1) so the domain stays ascending", () => {
    expect(relativeMax([])).toBe(1);
    expect(relativeMax([0, 0, 0])).toBe(1);
    expect(relativeMax([0.2, 0.5])).toBe(1);
  });

  it("honours a custom floor", () => {
    expect(relativeMax([2, 3], 10)).toBe(10);
    expect(relativeMax([20, 3], 10)).toBe(20);
  });

  it("ignores non-finite values", () => {
    expect(relativeMax([NaN, Infinity, 5, null, "x"])).toBe(5);
  });
});

describe("heatWeightExpression", () => {
  it("builds an interpolate from 0..max onto 0..1", () => {
    expect(heatWeightExpression(80)).toEqual([
      "interpolate",
      ["linear"],
      ["get", "heat"],
      0,
      0,
      80,
      1,
    ]);
  });
});

const NOW = Date.parse("2026-06-16T02:04:00Z");

describe("isUpcoming", () => {
  it("drops an hour once its clock hour has fully elapsed", () => {
    // Starts 01:00, ends 02:00, which is before now (02:04): elapsed.
    expect(isUpcoming("2026-06-16T01:00:00+00:00", NOW)).toBe(false);
  });

  it("keeps the in-progress hour and any future hour", () => {
    // Starts 02:00, ends 03:00 > now: still upcoming.
    expect(isUpcoming("2026-06-16T02:00:00+00:00", NOW)).toBe(true);
    expect(isUpcoming("2026-06-16T05:00:00+00:00", NOW)).toBe(true);
  });

  it("is timezone-safe: an offset-bearing string parses as an absolute instant", () => {
    // 03:00+01:00 == 02:00Z, in progress at 02:04Z.
    expect(isUpcoming("2026-06-16T03:00:00+01:00", NOW)).toBe(true);
  });

  it("keeps an unparseable time rather than silently dropping it", () => {
    expect(isUpcoming("not-a-date", NOW)).toBe(true);
  });
});

describe("nightKey", () => {
  it("folds pre-dawn hours back onto the evening that opened the night", () => {
    // 01:00Z and the prior 22:00Z belong to the same viewing night.
    expect(nightKey("2026-06-16T01:00:00+00:00")).toBe("2026-06-15");
    expect(nightKey("2026-06-15T22:00:00+00:00")).toBe("2026-06-15");
  });

  it("rolls to the next night once past midday UK time", () => {
    expect(nightKey("2026-06-16T12:00:00+00:00")).toBe("2026-06-16");
  });

  it("returns null for an unparseable time", () => {
    expect(nightKey("nope")).toBe(null);
  });
});

describe("liveWindows", () => {
  const site = {
    best_hours: [
      { time: "2026-06-16T00:00:00+00:00", dark: true }, // elapsed
      { time: "2026-06-16T03:00:00+00:00", dark: true }, // future dark
      { time: "2026-06-17T01:00:00+00:00", dark: false }, // future twilight-only
    ],
  };

  it("drops elapsed hours and keeps the clear-twilight superset (dark + twilight)", () => {
    const wins = liveWindows(site, NOW);
    expect(wins.map((h) => h.time)).toEqual([
      "2026-06-16T03:00:00+00:00",
      "2026-06-17T01:00:00+00:00",
    ]);
  });

  it("tolerates a missing site / best_hours", () => {
    expect(liveWindows(null, NOW)).toEqual([]);
    expect(liveWindows({}, NOW)).toEqual([]);
  });
});

describe("starsNights", () => {
  const sites = [
    {
      best_hours: [
        { time: "2026-06-16T00:00:00+00:00", dark: true }, // elapsed -> ignored
        { time: "2026-06-16T03:00:00+00:00", dark: true }, // night 2026-06-15
        { time: "2026-06-17T01:00:00+00:00", dark: false }, // night 2026-06-16, twilight
      ],
    },
    {
      best_hours: [
        { time: "2026-06-16T04:00:00+00:00", dark: true }, // night 2026-06-15 (dupe)
      ],
    },
  ];

  it("unions upcoming nights from true-dark windows in astronomical mode", () => {
    // The twilight-only window's night is excluded; the duplicate night folds.
    expect(starsNights(sites, "astronomical", NOW)).toEqual(["2026-06-15"]);
  });

  it("includes twilight windows' nights in twilight mode", () => {
    expect(starsNights(sites, "twilight", NOW)).toEqual([
      "2026-06-15",
      "2026-06-16",
    ]);
  });

  it("treats hours with no dark flag as dark (older payload)", () => {
    const legacy = [{ best_hours: [{ time: "2026-06-16T03:00:00+00:00" }] }];
    expect(starsNights(legacy, "astronomical", NOW)).toEqual(["2026-06-15"]);
  });

  it("returns an empty list for no sites", () => {
    expect(starsNights([], "twilight", NOW)).toEqual([]);
    expect(starsNights(undefined, "twilight", NOW)).toEqual([]);
  });
});
