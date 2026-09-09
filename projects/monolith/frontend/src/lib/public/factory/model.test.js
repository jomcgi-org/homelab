import { describe, expect, it } from "vitest";
import {
  activitySeries,
  factSeries,
  formatCount,
  formatSpend,
  goalSummary,
  markClass,
  modelLane,
  paginate,
  shortNumber,
  snapshotFreshness,
  sortPullRequests,
  spendSeries,
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
      { sessions: [], spend: [], merges: [], facts: factRows },
      "2026-09-07",
    );

    expect(factRows).toHaveLength(30);
    expect(factRows.at(-2)).toMatchObject({ v: 2, u: 1, n: 3 });
    expect(tiles.facts).toMatchObject({ value: 10 });
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
    const spend = spendSeries([
      { day: "2026-09-06", spend_usd: 700 },
      { day: "2026-09-07", spend_usd: 500 },
      { day: "2026-09-07", spend_usd: 34.4 },
    ]);
    const dual = tileDerivations(
      {
        totals_7d: {
          ember: {
            sessions: 2,
            input_tokens: 10,
            output_tokens: 4,
          },
          local: {
            sessions: 3,
            input_tokens: 20,
            output_tokens: 6,
          },
          combined: {
            sessions: 5,
            input_tokens: 30,
            output_tokens: 10,
            spend_usd: 1234.4,
          },
        },
      },
      { totals: {} },
      { daily: [], totals: {} },
      { sessions: shared, spend, merges: [], facts: [] },
      "2026-09-07",
    );
    const legacy = tileDerivations(
      { totals_7d: { sessions: 4, input_tokens: 7, output_tokens: 2 } },
      { totals: {} },
      { daily: [], totals: {} },
      { sessions: [], spend: [], merges: [], facts: [] },
      "2026-09-07",
    );

    expect(dual.sessions).toMatchObject({ value: 5 });
    expect(dual.tokens).toMatchObject({ input: 30 });
    expect(dual.spend.value).toBe(1234.4);
    expect(dual.spend.spark.at(-2)).toBe(700);
    expect(dual.spend.spark.at(-1)).toBe(534.4);
    expect(formatSpend(dual.spend.value)).toBe("$1.2k");
    expect(formatSpend(12.75)).toBe("$13");
    expect(legacy.sessions.value).toBe(4);
    expect(legacy.tokens.input).toBe(7);
  });
});

describe("number formatting", () => {
  it.each([
    [0, "0"],
    [999, "999"],
    [1_000, "1k"],
    [999_999, "1000k"],
    [1_000_000, "1.0M"],
    [1_000_000_000, "1.0B"],
    [1_500_000_000, "1.5B"],
    [1_000_000_000_000, "1.0T"],
  ])("formats %s as %s", (value, expected) => {
    expect(shortNumber(value)).toBe(expected);
  });

  it("formats a full count with locale separators", () => {
    expect(formatCount(1_234)).toBe("1,234");
    expect(formatCount(null)).toBe("0");
  });
});

describe("goalSummary", () => {
  const now = "2026-09-07T12:00:00Z";
  const row = (
    number,
    scope,
    type,
    mergedAt,
    additions = 1,
    deletions = 1,
  ) => ({
    number,
    scope,
    type,
    merged_at: mergedAt,
    additions,
    deletions,
    title: `${type}: pull ${number}`,
    agent_authored: true,
  });

  it("filters the window, orders areas, totals lines, and keeps recent titles", () => {
    const summary = goalSummary(
      [
        row(1, "beta", "fix", "2026-09-07T08:00:00Z"),
        row(2, "monolith", "feat", "2026-09-06T08:00:00Z", 10, 2),
        row(3, "alpha", "docs", "2026-09-07T09:00:00Z"),
        row(4, "monolith", "fix", "2026-09-07T10:00:00Z", 5, 3),
        row(5, "old", "fix", "2026-09-04T11:59:59Z"),
        row(6, "future", "feat", "2026-09-07T12:00:01Z"),
      ],
      now,
    );

    expect(summary).toMatchObject({ windowHours: 72, total: 4 });
    expect(summary.goals.map((goal) => goal.area)).toEqual([
      "monolith",
      "alpha",
      "beta",
    ]);
    expect(summary.goals[0]).toMatchObject({
      merged: 2,
      share: 0.5,
      types: [
        ["feat", 1],
        ["fix", 1],
      ],
      additions: 15,
      deletions: 5,
      recent: [
        { number: 4, title: "pull 4" },
        { number: 2, title: "pull 2" },
      ],
    });
  });

  it.each([
    ["hardening", ["fix", "fix", "fix", "feat"]],
    ["building", ["feat", "feat", "feat", "docs"]],
    ["documenting", ["docs", "docs", "docs", "fix"]],
    ["maintaining", ["chore", "chore", "chore", "fix"]],
    ["building and hardening", ["fix", "fix", "feat", "docs"]],
    ["mixed work", ["docs", "feat", "chore", "test"]],
  ])("classifies %s", (expected, types) => {
    const summary = goalSummary(
      types.map((type, index) =>
        row(index + 1, "area", type, `2026-09-07T0${index}:00:00Z`),
      ),
      now,
    );
    expect(summary.goals[0].focus).toBe(expected);
  });

  it("normalizes an empty scope and respects custom limits", () => {
    const summary = goalSummary(
      [
        row(1, "", "fix", "2026-09-07T10:00:00Z"),
        row(2, "beta", "fix", "2026-09-07T10:00:00Z"),
      ],
      now,
      { windowHours: 24, limit: 1 },
    );
    expect(summary.windowHours).toBe(24);
    expect(summary.goals).toHaveLength(1);
    expect(summary.goals[0].area).toBe("beta");
  });

  it("returns an empty summary for missing work", () => {
    expect(goalSummary(undefined, now)).toEqual({
      windowHours: 72,
      total: 0,
      goals: [],
    });
    expect(goalSummary([], now)).toEqual({
      windowHours: 72,
      total: 0,
      goals: [],
    });
  });
});

describe("snapshotFreshness", () => {
  const snapshot = "2026-09-07T14:32:45Z";

  it.each([
    ["2026-09-07T14:33:14Z", 0, "just now", false],
    ["2026-09-07T14:44:45Z", 12, "12 min ago", false],
    ["2026-09-07T15:36:45Z", 64, "1 h 04 min ago", false],
    ["2026-09-07T16:02:45Z", 90, "1 h 30 min ago", false],
    ["2026-09-07T16:03:45Z", 91, "1 h 31 min ago", true],
  ])("formats freshness at %s", (now, minutes, label, stale) => {
    expect(snapshotFreshness(snapshot, now)).toEqual({
      iso: snapshot,
      clock: "14:32",
      minutes,
      label,
      stale,
    });
  });

  it("marks a missing snapshot unknown and stale", () => {
    expect(snapshotFreshness(null, "2026-09-07T16:00:00Z")).toEqual({
      iso: null,
      clock: null,
      minutes: null,
      label: "unknown",
      stale: true,
    });
  });
});
