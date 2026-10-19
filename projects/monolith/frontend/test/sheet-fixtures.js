// Invented, deterministic report shared by mounted page and browser tests.
const model = (id, extra = {}) => ({
  id,
  role: "candidate",
  qualified: true,
  self_hosted: false,
  hard_pass: 1,
  hard_n: 1,
  floor_pass: 2,
  floor_n: 2,
  cost_usd: 0.02,
  cost_per_solve_usd: 0.03,
  mean_latency_ms: 2400,
  mean_tokens: 1200,
  mean_turns: 4,
  tool_use_ok: 1,
  tasks: [
    { id: "fixture-easy", passed: true },
    { id: "fixture-standard", passed: true },
    { id: "fixture-hard", passed: true },
  ],
  errored_tasks: [],
  ...extra,
});

export const leaderboard = {
  generated_at: "2026-10-19",
  harness_version: "synthetic",
  models: [
    model("anthropic/fixture-anchor", {
      role: "anchor",
      cost_usd: 0.6,
      cost_per_solve_usd: 0.8,
      mean_latency_ms: 1200,
    }),
    model("qwen/fixture-local", {
      self_hosted: true,
      cost_usd: 0,
      cost_per_solve_usd: 0,
      hard_pass: 0,
      mean_latency_ms: 4800,
      tasks: [{ id: "fixture-easy", passed: true }],
      errored_tasks: ["fixture-standard"],
    }),
    model("deepseek/fixture-cloud"),
    model("cohere/fixture-unqualified", {
      qualified: false,
      hard_pass: 0,
      cost_usd: 0.01,
      tasks: [{ id: "fixture-hard", passed: false }],
    }),
  ],
  tasks: [
    {
      id: "fixture-easy",
      tier: "easy",
      real_test: false,
      blurb: "Invented easy task",
      passed: 3,
      n: 3,
    },
    {
      id: "fixture-standard",
      tier: "standard",
      real_test: true,
      blurb: "Invented repository task",
      passed: 2,
      n: 2,
    },
    {
      id: "fixture-hard",
      tier: "hard",
      real_test: true,
      blurb: "Invented hard task",
      passed: 2,
      n: 3,
    },
  ],
  index: {
    min_n: 3,
    roles: { worker: {}, reviewer: {} },
    models: {
      "deepseek/fixture-cloud": {
        roles: {
          worker: { index: 0.8, ci: [0.6, 0.9], coverage: 1 },
          reviewer: { index: 0.6, ci: [0.4, 0.8], coverage: 1 },
        },
        low_n: ["fixture"],
      },
    },
    picks: {
      worker: {
        best: "deepseek/fixture-cloud",
        cheapest: "deepseek/fixture-cloud",
        fastest: "deepseek/fixture-cloud",
      },
    },
  },
};
