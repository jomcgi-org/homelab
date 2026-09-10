import { describe, expect, it } from "vitest";
import {
  budgetShare,
  conductorHref,
  conductorPrompt,
  deadlineLabel,
  focusNode,
  laneSummary,
  phaseLabel,
  planRanks,
  turnShare,
} from "./factory-view.js";

const receipt = {
  task_id: "t-1",
  issue_number: 5980,
  title: "probes park their guest",
  state: "admitted",
  task_paused: false,
  turns_used: 3,
  committed_cost_usd: 9,
  deadline_at: "2026-09-10T17:00:00Z",
  policy: { max_turns_per_task: 9, task_budget_usd: 36 },
  nodes: [
    {
      node_key: "conductor_1",
      label: "conductor · 1",
      state: "done",
      deps: [],
      model: "spark",
    },
    {
      node_key: "investigate_x",
      label: "investigate · x",
      state: "done",
      deps: ["conductor_1"],
      model: "sonnet",
    },
    {
      node_key: "implement_y",
      label: "implement · y",
      state: "running",
      deps: ["investigate_x"],
      model: "sonnet",
    },
  ],
};

describe("factory board helpers", () => {
  it("ranks the plan by dependencies", () => {
    const ranks = planRanks(receipt.nodes);
    expect(ranks.map((r) => r.map((n) => n.key))).toEqual([
      ["conductor_1"],
      ["investigate_x"],
      ["implement_y"],
    ]);
  });

  it("clamps budget and turn shares", () => {
    expect(budgetShare(receipt)).toBeCloseTo(0.25);
    expect(turnShare(receipt)).toBeCloseTo(1 / 3);
    expect(budgetShare({ policy: {}, committed_cost_usd: 1 })).toBe(0);
    expect(
      turnShare({ policy: { max_turns_per_task: 2 }, turns_used: 5 }),
    ).toBe(1);
  });

  it("labels the deadline in both directions", () => {
    const now = Date.parse("2026-09-10T14:30:00Z");
    expect(deadlineLabel(receipt, now)).toBe("2h 30m left");
    expect(deadlineLabel({ deadline_at: "2026-09-10T14:10:00Z" }, now)).toBe(
      "overdue 20m",
    );
    expect(deadlineLabel({}, now)).toBe("");
  });

  it("focuses attention, then the running node", () => {
    expect(focusNode(receipt.nodes).node_key).toBe("implement_y");
    const withFailure = [
      ...receipt.nodes,
      { node_key: "review_z", state: "uncertain" },
    ];
    expect(focusNode(withFailure).node_key).toBe("review_z");
    expect(focusNode([])).toBeNull();
  });

  it("phrases the phase from the focus node and the receipt state", () => {
    expect(phaseLabel(receipt)).toBe("running · implement · y");
    expect(phaseLabel({ ...receipt, task_paused: true })).toBe(
      "paused · implement · y",
    );
    expect(phaseLabel({ state: "queued" })).toBe("waiting for a slot");
    expect(phaseLabel({ state: "succeeded" })).toBe("landed");
    expect(phaseLabel({ state: "admitted", nodes: [] })).toBe("planning");
  });

  it("summarises the lane for the launcher strip", () => {
    expect(
      laneSummary({
        state: "enabled",
        active: [receipt, { ...receipt, task_paused: true }],
        queued: [{}],
      }),
    ).toEqual({ state: "enabled", active: 2, queued: 1, paused: 1 });
    expect(laneSummary(null)).toEqual({
      state: "unknown",
      active: 0,
      queued: 0,
      paused: 0,
    });
  });

  it("writes a conductor prompt that names the task and its plan", () => {
    const prompt = conductorPrompt(receipt);
    expect(prompt).toContain("task t-1 (#5980: probes park their guest)");
    expect(prompt).toContain("implement_y [running, sonnet]");
    expect(conductorPrompt(null)).toContain("new proposal");
  });

  it("links into the console with the model and prompt prefilled", () => {
    const href = conductorHref(receipt);
    const url = new URL(href, "https://private.jomcgi.dev");
    expect(url.pathname).toBe("/agents");
    expect(url.searchParams.get("compose")).toBe("1");
    expect(url.searchParams.get("model")).toBe("astra");
    expect(url.searchParams.get("prompt")).toContain("t-1");
  });
});
