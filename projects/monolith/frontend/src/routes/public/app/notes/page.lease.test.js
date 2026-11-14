import { afterEach, describe, expect, it, vi } from "vitest";
import { load } from "./+page.js";

vi.mock("$app/environment", () => ({ browser: true }));
afterEach(() => vi.unstubAllGlobals());

const overview = { leaseToken: "response-a", leaseMs: 10_000, expiresAt: 123 };
const entry = (token, startTime) => ({
  startTime,
  serverTiming: [{ name: "notes-lease", description: token }],
});
function timing(navigation = [], resource = []) {
  vi.stubGlobal("performance", {
    getEntriesByType: (type) => ({ navigation, resource })[type],
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
  });

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
