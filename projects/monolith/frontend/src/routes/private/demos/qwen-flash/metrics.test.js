import { describe, expect, it } from "vitest";
import {
  addSessionTurn,
  attributeExpertActivity,
  calculateTierSummary,
  calculateTurnMetrics,
  classifyLayerTier,
  countTurnTokens,
  deriveModelState,
  diffExpertHits,
  formatBytes,
  formatRate,
  trackSessionPeaks,
} from "./metrics.js";

describe("trackSessionPeaks", () => {
  it("tracks the fastest decode rate and lowest first-token time", () => {
    const first = trackSessionPeaks(
      { decodeTps: 0, ttftMs: null },
      { tokensPerSecond: 18.2, ttftMs: 2_400 },
    );
    const second = trackSessionPeaks(first, {
      tokensPerSecond: 21.4,
      ttftMs: 2_900,
    });

    expect(second).toEqual({ decodeTps: 21.4, ttftMs: 2_400 });
  });

  it("ignores a turn before its first token arrives", () => {
    expect(
      trackSessionPeaks(
        { decodeTps: 20, ttftMs: 1_500 },
        { tokensPerSecond: 0, ttftMs: null },
      ),
    ).toEqual({ decodeTps: 20, ttftMs: 1_500 });
  });
});

describe("addSessionTurn", () => {
  it("adds turns, generated tokens, and generation time", () => {
    expect(
      addSessionTurn(
        { turns: 2, tokens: 40, generationMs: 12_000 },
        { reasoningTokens: 7, answerTokens: 13 },
        4_500,
      ),
    ).toEqual({ turns: 3, tokens: 60, generationMs: 16_500 });
  });

  it("sanitizes missing turn metrics", () => {
    expect(addSessionTurn(undefined, undefined, undefined)).toEqual({
      turns: 1,
      tokens: 0,
      generationMs: 0,
    });
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

describe("classifyLayerTier", () => {
  it("accepts arrays and treats malformed values as unknown", () => {
    const layers = [0, 2.9, -1, "3.6", null];

    expect(classifyLayerTier(layers, 0)).toBe("resident");
    expect(classifyLayerTier(layers, 1)).toBe("disk");
    expect(classifyLayerTier(layers, 2)).toBe("unknown");
    expect(classifyLayerTier(layers, 3)).toBe("unknown");
    expect(classifyLayerTier(layers, 4)).toBe("unknown");
    expect(classifyLayerTier(layers, 5)).toBe("unknown");
  });
});

describe("diffExpertHits", () => {
  it("does not report existing cumulative counts on the first poll", () => {
    expect(diffExpertHits(null, { expert_hits: [[10, 20, 30]] })).toEqual([]);
  });

  it("reports only counters that increased and includes their deltas", () => {
    expect(
      diffExpertHits(
        {
          expert_hits: [
            [10, 20, 30],
            [5, 6],
          ],
        },
        {
          expert_hits: [
            [10, 23, 29],
            [7, 6],
          ],
        },
      ),
    ).toEqual([
      { layer: 0, expert: 1, delta: 3 },
      { layer: 1, expert: 0, delta: 2 },
    ]);
  });
});

describe("calculateTierSummary", () => {
  const geometry = {
    num_experts: 512,
    num_moe_layers: 48,
    moe_cache_size: 3_753,
    unit_bytes: { moe_per_expert: 2_772_480 },
  };

  it("derives all three tiers from the layer map and cache geometry", () => {
    const profile = { layers: [...Array(28).fill(0), ...Array(20).fill(1.2)] };

    expect(calculateTierSummary(profile, geometry)).toEqual({
      totalExperts: 24_576,
      residentLayers: 28,
      diskLayers: 20,
      hotExperts: 3_753,
      warmExperts: 14_336,
      coldExperts: 6_487,
      hotBytes: 10_405_117_440,
      warmBytes: 39_746_273_280,
      coldBytes: 17_985_077_760,
    });
  });

  it("puts every expert in RAM when the map has no disk layers", () => {
    const profile = { layers: Array(48).fill(0) };

    expect(calculateTierSummary(profile, geometry)).toMatchObject({
      totalExperts: 24_576,
      residentLayers: 48,
      diskLayers: 0,
      hotExperts: 0,
      warmExperts: 24_576,
      coldExperts: 0,
    });
  });

  it("preserves derivable counts when geometry fields are missing", () => {
    const profile = {
      layers: [0, 1],
      expert_hits: [
        [1, 2, 3],
        [4, 5, 6],
      ],
    };

    expect(calculateTierSummary(profile, {})).toEqual({
      totalExperts: 6,
      residentLayers: 1,
      diskLayers: 1,
      hotExperts: null,
      warmExperts: 3,
      coldExperts: null,
      hotBytes: null,
      warmBytes: null,
      coldBytes: null,
    });
  });
});

describe("attributeExpertActivity", () => {
  it("attributes hit deltas to warm and likely hot or cold experts", () => {
    const geometry = {
      num_experts: 4,
      num_moe_layers: 2,
      moe_cache_size: 2,
    };
    const previous = {
      layers: [0, 1],
      expert_hits: [
        [0, 1, 0, 1],
        [5, 30, 20, 10],
      ],
    };
    const current = {
      layers: [0, 1],
      expert_hits: [
        [2, 1, 0, 4],
        [5, 34, 23, 12],
      ],
    };

    expect(attributeExpertActivity(previous, current, geometry)).toEqual({
      hotHits: 7,
      warmHits: 5,
      coldHits: 2,
      unknownHits: 0,
      totalHits: 14,
    });
  });

  it("marks disk hits unknown when cache geometry is unavailable", () => {
    const previous = { layers: [1], expert_hits: [[1, 1]] };
    const current = { layers: [1], expert_hits: [[3, 2]] };

    expect(attributeExpertActivity(previous, current, {})).toEqual({
      hotHits: 0,
      warmHits: 0,
      coldHits: 0,
      unknownHits: 3,
      totalHits: 3,
    });
  });
});
