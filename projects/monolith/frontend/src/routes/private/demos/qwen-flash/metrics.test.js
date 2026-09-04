import { describe, expect, it } from "vitest";
import {
  calculateTurnMetrics,
  deriveModelState,
  formatBytes,
  formatRate,
  trackPeaks,
} from "./metrics.js";

describe("trackPeaks", () => {
  it("replaces a peak with a new maximum", () => {
    expect(
      trackPeaks(
        { decodeTps: 18.2, prefillTps: 320.1 },
        { throughput: { decode_tps: 21.4, prefill_tps: 389.6 } },
      ),
    ).toEqual({ decodeTps: 21.4, prefillTps: 389.6 });
  });

  it("keeps the peak when a later sample is lower", () => {
    expect(
      trackPeaks(
        { decodeTps: 21.4, prefillTps: 389.6 },
        { throughput: { decode_tps: 17.8, prefill_tps: 201.2 } },
      ),
    ).toEqual({ decodeTps: 21.4, prefillTps: 389.6 });
  });
});

describe("deriveModelState", () => {
  it.each([
    [false, false, "idle"],
    [false, true, "idle"],
    [true, false, "prefilling"],
    [true, true, "generating"],
  ])("maps %s and %s to %s", (inFlight, firstTokenSeen, expected) => {
    expect(deriveModelState(inFlight, firstTokenSeen)).toBe(expected);
  });
});

describe("calculateTurnMetrics", () => {
  it("measures first token latency and decode rate from timestamped chunks", () => {
    expect(
      calculateTurnMetrics(1000, [
        { at: 2500, tokens: 1 },
        { at: 2750, tokens: 1 },
        { at: 3000, tokens: 1 },
      ]),
    ).toEqual({ ttftMs: 1500, tokensPerSecond: 4 });
  });

  it("reports no rate until two streamed tokens establish an interval", () => {
    expect(calculateTurnMetrics(1000, [{ at: 1600, tokens: 1 }])).toEqual({
      ttftMs: 600,
      tokensPerSecond: 0,
    });
  });
});

describe("metric formatting", () => {
  it("formats byte values as fixed-width decimal gigabytes", () => {
    expect(formatBytes(22_988_980_224)).toBe("23.0 GB");
    expect(formatBytes(undefined)).toBe("0.0 GB");
  });

  it("formats rates to one decimal place", () => {
    expect(formatRate(18)).toBe("18.0");
    expect(formatRate(18.26)).toBe("18.3");
    expect(formatRate(undefined)).toBe("0.0");
  });
});
