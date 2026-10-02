import { describe, expect, it } from "vitest";
import {
  cellState,
  matchPreset,
  money,
  orderedTasks,
  paretoFrontier,
  parseSelection,
  presetIds,
  providerSlot,
  rank,
  serializeSelection,
  sortByMetric,
} from "./model.js";

const model = (id, extra = {}) => ({
  id,
  role: "candidate",
  qualified: true,
  self_hosted: false,
  hard_pass: 7,
  hard_n: 7,
  cost_usd: 0.01,
  cost_per_solve_usd: 0.01,
  mean_latency_ms: 1000,
  mean_tokens: 1000,
  mean_turns: 5,
  tasks: [],
  errored_tasks: [],
  ...extra,
});

const MODELS = [
  model("anthropic/claude-opus", {
    role: "anchor",
    cost_usd: 0.7,
    cost_per_solve_usd: 0.7,
  }),
  model("qwen/local", {
    self_hosted: true,
    cost_usd: 0,
    cost_per_solve_usd: 0,
    hard_pass: 6,
  }),
  model("deepseek/a", { cost_per_solve_usd: 0.02 }),
  model("deepseek/b", { cost_per_solve_usd: 0.03 }),
  model("z-ai/c", { cost_per_solve_usd: 0.04 }),
  model("z-ai/d", { cost_per_solve_usd: 0.05 }),
  model("z-ai/e", { cost_per_solve_usd: 0.06 }),
  model("cohere/dq", { qualified: false }),
];

describe("rank", () => {
  it("ranks by hard-task rate, not count", () => {
    const more = model("a/more", { hard_pass: 8, hard_n: 9 });
    const all = model("a/all", { hard_pass: 7, hard_n: 7 });
    expect(rank([more, all]).map((m) => m.id)).toEqual(["a/all", "a/more"]);
  });

  it("puts disqualified models last", () => {
    expect(rank(MODELS).at(-1).id).toBe("cohere/dq");
  });
});

describe("presets", () => {
  it("defaults to anchors, self-hosted, and the top four rented models", () => {
    expect([...presetIds(MODELS, "default")].sort()).toEqual(
      [
        "anthropic/claude-opus",
        "qwen/local",
        "deepseek/a",
        "deepseek/b",
        "z-ai/c",
        "z-ai/d",
      ].sort(),
    );
  });

  it("recognises a selection that matches a preset", () => {
    expect(matchPreset(MODELS, presetIds(MODELS, "all"))).toBe("all");
    expect(matchPreset(MODELS, new Set(["z-ai/e"]))).toBeNull();
  });

  it("round-trips a custom selection through the URL", () => {
    const ids = new Set(["z-ai/e", "deepseek/a"]);
    const raw = serializeSelection(MODELS, ids);
    expect(parseSelection(MODELS, raw)).toEqual(ids);
  });

  it("leaves the URL clean for the default and ignores unknown ids", () => {
    expect(serializeSelection(MODELS, presetIds(MODELS, "default"))).toBeNull();
    expect(parseSelection(MODELS, "nope/x")).toEqual(
      presetIds(MODELS, "default"),
    );
  });
});

describe("sortByMetric", () => {
  it("does not reorder the caller's array", () => {
    const input = [...MODELS];
    sortByMetric(input, "cost");
    expect(input).toEqual(MODELS);
  });

  it("leaves the anchors out of harness-specific metrics", () => {
    expect(
      sortByMetric(MODELS, "tokens").some((m) => m.role === "anchor"),
    ).toBe(false);
  });
});

describe("cells and tasks", () => {
  it("separates errored and unrun cells from failures", () => {
    const m = model("a/x", {
      tasks: [
        { id: "t1", passed: true },
        { id: "t2", passed: false },
        { id: "t3", passed: false },
      ],
      errored_tasks: ["t3"],
    });
    expect(["t1", "t2", "t3", "t4"].map((t) => cellState(m, t))).toEqual([
      "pass",
      "fail",
      "errored",
      "none",
    ]);
  });

  it("orders tasks easy, standard, hard and numbers them", () => {
    const out = orderedTasks([
      { id: "b", tier: "hard" },
      { id: "a", tier: "easy" },
      { id: "c", tier: "standard" },
    ]);
    expect(out.map((t) => [t.id, t.no])).toEqual([
      ["a", 1],
      ["c", 2],
      ["b", 3],
    ]);
  });
});

describe("formatting", () => {
  it("keeps provider slots fixed and folds unknowns", () => {
    expect(providerSlot("qwen/x")).toBe("1");
    expect(providerSlot("anthropic/x")).toBe("2");
    expect(providerSlot("mystery/x")).toBe("other");
  });

  it("formats money by magnitude", () => {
    expect([money(0), money(0.0053), money(0.045), money(1)]).toEqual([
      "$0",
      "$0.0053",
      "$0.045",
      "$1.00",
    ]);
  });
});

describe("paretoFrontier", () => {
  it("keeps only points nothing beats on both axes, left to right", () => {
    const pts = [
      { id: "cheap-weak", x: 1, y: 0.5 },
      { id: "mid", x: 2, y: 0.8 },
      { id: "dominated", x: 3, y: 0.7 },
      { id: "best", x: 4, y: 1 },
      { id: "tie-worse", x: 5, y: 1 },
    ];
    expect(paretoFrontier(pts).map((p) => p.id)).toEqual([
      "cheap-weak",
      "mid",
      "best",
    ]);
  });

  it("collapses to one point when one model wins outright", () => {
    const pts = [
      { id: "free-perfect", x: 0, y: 1 },
      { id: "paid", x: 1, y: 1 },
    ];
    expect(paretoFrontier(pts).map((p) => p.id)).toEqual(["free-perfect"]);
  });
});
