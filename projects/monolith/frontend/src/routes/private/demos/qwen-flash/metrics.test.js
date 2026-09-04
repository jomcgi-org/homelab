import { describe, expect, it } from "vitest";
import {
  calculateTurnMetrics,
  countTurnTokens,
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
  it("measures first token latency and decode rate from answer chunks", () => {
    expect(
      calculateTurnMetrics(1000, [
        { at: 2500, role: "assistant" },
        { at: 2500, content: "One" },
        { at: 2750, content: " two" },
        { at: 3000, content: " three" },
      ]),
    ).toEqual({
      ttftMs: 1500,
      tokensPerSecond: 4,
      timeToFirstReasoningMs: null,
      timeToFirstAnswerMs: 1500,
      reasoningTokens: 0,
      answerTokens: 3,
    });
  });

  it("reports no rate until two streamed tokens establish an interval", () => {
    expect(
      calculateTurnMetrics(1000, [{ at: 1600, content: "Hello" }]),
    ).toEqual({
      ttftMs: 600,
      tokensPerSecond: 0,
      timeToFirstReasoningMs: null,
      timeToFirstAnswerMs: 600,
      reasoningTokens: 0,
      answerTokens: 1,
    });
  });

  it("derives reasoning and answer timings and counts from streamed deltas", () => {
    expect(
      calculateTurnMetrics(1000, [
        { at: 1050, role: "assistant" },
        { at: 4000, reasoning_content: "We" },
        { at: 4100, reasoning_content: " think" },
        { at: 5300, content: "The" },
        { at: 5400, content: " answer" },
      ]),
    ).toEqual({
      ttftMs: 3000,
      tokensPerSecond: 3_000 / 1_400,
      timeToFirstReasoningMs: 3000,
      timeToFirstAnswerMs: 4300,
      reasoningTokens: 2,
      answerTokens: 2,
    });
  });
});

describe("countTurnTokens", () => {
  it("counts reasoning separately from answer and ignores the role chunk", () => {
    expect(
      countTurnTokens([
        { role: "assistant" },
        { reasoning_content: "First" },
        { reasoning_content: " thought" },
        { content: "Answer" },
      ]),
    ).toEqual({ reasoningTokens: 2, answerTokens: 1 });
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
