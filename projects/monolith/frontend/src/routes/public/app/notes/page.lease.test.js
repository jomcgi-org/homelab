import { afterEach, describe, expect, it, vi } from "vitest";
import { load } from "./+page.js";
import { overviewLeaseCurrent } from "$lib/public/notes/overview-lease.js";

vi.mock("$app/environment", () => ({ browser: true }));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const overview = { leaseToken: "response-a", leaseMs: 10_000, expiresAt: 123 };
const entry = (token, startTime) => ({
  startTime,
  serverTiming: [{ name: "notes-lease", description: token }],
});
function timing(navigation = [], resource = []) {
  vi.stubGlobal("performance", {
    getEntriesByType: (type) => ({ navigation, resource })[type],
    now: () => 1000,
    timeOrigin: 1_000_000,
  });
}

describe("Notes browser overview lease", () => {
  it("consumes initial request time without using the browser wall clock", () => {
    timing([entry("response-a", 0)]);
    expect(load({ data: { overview } }).overviewDeadline).toBe(10_000);
  });

  it("keeps a prefetched response anchored to its own request", () => {
    timing([], [entry("response-a", 50_000), entry("response-b", 80_000)]);
    // A later navigation or request cannot refresh the prefetched response.
    const first = load({ data: { overview } });
    const later = load({ data: { overview } });
    expect(first.overviewDeadline).toBe(60_000);
    expect(later.overviewDeadline).toBe(60_000);
    expect(later.overviewClock).toEqual(first.overviewClock);
  });

  it("does not replace the relative clock anchor when prefetched data is reused after sleep", () => {
    timing([], [entry("prefetched", 0)]);
    const data = { overview: { ...overview, leaseToken: "prefetched" } };
    const wall = vi.spyOn(Date, "now").mockReturnValue(1_001_000);
    const first = load({ data });
    expect(
      overviewLeaseCurrent(first.overviewDeadline, first.overviewClock),
    ).toBe(true);
    wall.mockReturnValue(1_060_000);
    const restored = load({ data });
    expect(restored.overviewClock).toEqual(first.overviewClock);
    expect(
      overviewLeaseCurrent(restored.overviewDeadline, restored.overviewClock),
    ).toBe(false);
  });

  it("rejects sleep while the response or hydration was pending, before the first load call", () => {
    timing([entry("pending", 0)]);
    vi.spyOn(Date, "now").mockReturnValue(1_060_000);
    const data = load({
      data: { overview: { ...overview, leaseToken: "pending" } },
    });
    expect(data.overviewClock).toEqual({ monotonicAt: 0, wallAt: 1_000_000 });
    expect(
      overviewLeaseCurrent(data.overviewDeadline, data.overviewClock),
    ).toBe(false);
  });

  it("fails closed when the browser lacks a document clock origin", () => {
    timing();
    performance.timeOrigin = undefined;
    const data = load({ data: { overview } });
    expect(data.overviewClock).toBeNull();
    expect(
      overviewLeaseCurrent(data.overviewDeadline, data.overviewClock),
    ).toBe(false);
  });

  it.each([1_000, 9_000_000_000_000])(
    "cancels initial clock offset against the document origin: %s",
    (origin) => {
      timing([entry("response-a", 0)]);
      performance.timeOrigin = origin;
      vi.spyOn(Date, "now").mockReturnValue(origin + 1000);
      const data = load({ data: { overview } });
      expect(
        overviewLeaseCurrent(data.overviewDeadline, data.overviewClock),
      ).toBe(true);
    },
  );

  it.each([1_000, 9_000_000_000_000])(
    "accepts constant browser wall-clock skew: %s",
    (wallAt) => {
      const clock = { monotonicAt: 200, wallAt };
      expect(overviewLeaseCurrent(10_200, clock, 1200, wallAt + 1000)).toBe(
        true,
      );
      expect(overviewLeaseCurrent(10_200, clock, 10_200, wallAt + 10_000)).toBe(
        false,
      );
    },
  );

  it.each([
    [200, 1_020_000], // monotonic time paused through sleep
    [1200, 999_000], // browser wall clock moved backwards
    [1200, 1_005_000], // clocks diverged before either lease expires
  ])(
    "fails closed for clock discontinuity (%s, %s)",
    (monotonicNow, wallNow) => {
      expect(
        overviewLeaseCurrent(
          10_200,
          { monotonicAt: 200, wallAt: 1_000_000 },
          monotonicNow,
          wallNow,
        ),
      ).toBe(false);
    },
  );

  it("uses document start when timing is missing, never navigation time", () => {
    timing([], [{ startTime: 90_000 }, entry("other-response", 80_000)]);
    expect(load({ data: { overview } }).overviewDeadline).toBe(10_000);
  });

  it.each([
    null,
    {},
    { leaseMs: Infinity, leaseToken: "response-a" },
    { leaseMs: -1, leaseToken: "response-a" },
  ])("fails closed for a missing or invalid lease: %s", (overview) => {
    timing();
    expect(load({ data: { overview } }).overviewDeadline).toBe(0);
  });
});
