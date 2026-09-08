import { describe, expect, it } from "vitest";
import {
  activitySeries,
  factSeries,
  markClass,
  modelLane,
  paginate,
  sortPullRequests,
  tileDerivations,
} from "./model.js";

describe("modelLane", () => {
  it.each([
    ["luna", "luna"],
    ["terra", "codex"],
    ["sol", "codex"],
    ["opus", "claude"],
    ["sonnet", "claude"],
    ["fable", "claude"],
    ["spark", "spark"],
    ["pi-spark", "spark"],
    ["claude-opus-5", "claude"],
    ["gpt-5.6-luna", "luna"],
    ["gpt-5.6-terra", "codex"],
    ["gpt-5.6-sol", "codex"],
    ["codex-auto-review", "codex"],
    ["<synthetic>", "other"],
    ["muse-spark-1.3-contributor", "spark"],
    ["muse", "spark"],
    ["qwen", "other"],
    ["unknown", "other"],
    [null, "other"],
  ])("maps %s to %s", (model, lane) => {
    expect(modelLane(model)).toBe(lane);
  });

  it("buckets activity with the explicit model map", () => {
    const rows = activitySeries([
      { day: "2026-09-07", model: "opus", sessions: 2 },
      { day: "2026-09-07", model: "qwen", sessions: 1 },
    ]);
    expect(rows[0]).toMatchObject({ claude: 2, other: 1, sessions: 3 });
  });

  it("stacks Ember and local rows into the same lane totals", () => {
    const rows = activitySeries([
      { day: "2026-09-07", model: "luna", sessions: 2, source: "ember" },
      {
        day: "2026-09-07",
        model: "gpt-5.6-luna",
        sessions: 3,
        source: "codex-session",
      },
    ]);
    expect(rows[0]).toMatchObject({ luna: 5, sessions: 5 });
  });
});

describe("pull request table", () => {
  const rows = [
    {
      number: 1,
      merged_at: "2026-09-01",
      type: "fix",
      additions: 2,
      deletions: 1,
    },
    {
      number: 2,
      merged_at: "2026-09-02",
      type: "feat",
      additions: 9,
      deletions: 2,
    },
  ];

  it("sorts by the selected field and direction", () => {
    expect(
      sortPullRequests(rows, "lines", -1).map((row) => row.number),
    ).toEqual([2, 1]);
  });

  it("paginates and clamps an out-of-range page", () => {
    expect(paginate(rows, 9, 1)).toMatchObject({
      rows: [rows[1]],
      page: 1,
      pageCount: 2,
      start: 2,
      end: 2,
    });
  });
});

describe("fact and tile derivations", () => {
  it("builds a today-anchored fact series and shared fact tile totals", () => {
    const facts = {
      daily: [{ d: "2026-09-06", verified: 2, unverified: 1 }],
      totals: { verified: 7, unverified: 3 },
    };
    const factRows = factSeries(facts, "2026-09-07");
    const tiles = tileDerivations(
      { now: {}, totals_7d: {} },
      { totals: {} },
      facts,
      { sessions: [], merges: [], lines: [], facts: factRows },
      "2026-09-07",
    );

    expect(factRows).toHaveLength(30);
    expect(factRows.at(-2)).toMatchObject({ v: 2, u: 1, n: 3 });
    expect(tiles.facts).toMatchObject({ value: 10, verified: 7 });
  });

  it("selects the invalidated hatch and disputed outline marks", () => {
    expect(markClass({ verification_state: "invalidated" })).toBe(
      "invalidated",
    );
    expect(markClass({ verification_state: "verified", disputed: true })).toBe(
      "disputed",
    );
  });

  it("combines dual-source activity totals and accepts legacy flat totals", () => {
    const shared = [{ d: "2026-09-07", sessions: 5, input_tokens: 12 }];
    const dual = tileDerivations(
      {
        totals_7d: {
          ember: {
            sessions: 2,
            input_tokens: 10,
            output_tokens: 4,
            cost_usd: 1.25,
            list_cost_usd: 2,
          },
          local: {
            sessions: 3,
            input_tokens: 20,
            output_tokens: 6,
            list_cost_usd: 3,
          },
          combined: {
            sessions: 5,
            input_tokens: 30,
            output_tokens: 10,
            cost_usd: 1.25,
            list_cost_usd: 5,
          },
        },
      },
      { totals: {} },
      { daily: [], totals: {} },
      { sessions: shared, merges: [], lines: [], facts: [] },
      "2026-09-07",
    );
    const legacy = tileDerivations(
      { totals_7d: { sessions: 4, input_tokens: 7, output_tokens: 2 } },
      { totals: {} },
      { daily: [], totals: {} },
      { sessions: [], merges: [], lines: [], facts: [] },
      "2026-09-07",
    );

    expect(dual.sessions).toMatchObject({ value: 5, ember: 2, local: 3 });
    expect(dual.tokens).toMatchObject({ input: 30, output: 10 });
    expect(dual.cost).toMatchObject({ metered: 1.25, list: 5 });
    expect(legacy.sessions.value).toBe(4);
    expect(legacy.tokens.input).toBe(7);
  });
});
