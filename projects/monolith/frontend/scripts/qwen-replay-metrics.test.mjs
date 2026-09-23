import { describe, expect, it } from "vitest";
import {
  attributeExpertActivity,
  calculateTierSummary,
} from "./qwen-replay-metrics.mjs";

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
