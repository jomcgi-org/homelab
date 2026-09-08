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
});
