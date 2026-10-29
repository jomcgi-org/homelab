// Invented data only. No IDs, notes or decisions are loaded from production.
export const itemId = "100001";
export const taskId =
  "t-synthetic-78381e85-5d08-4751-a77a-long-operator-investigation";
export const routes = {
  board: "/factory",
  escalations: "/factory/escalations",
  item: `/factory/work-items/${itemId}`,
};
const at = "2026-10-19T09:00:00Z";
const policy = {
  generation: 3,
  conductor_model: "synthetic-planner",
  worker_model: "synthetic-worker",
  reviewer_model: "synthetic-reviewer",
  max_task_turns_hard: 9,
  task_budget_usd: 36,
};
export function receipt(overrides = {}) {
  return {
    id: 11,
    issue_number: 90001,
    generation: 3,
    title:
      "Synthetic: preserve operator confirmation through reordered polling and long investigation identifiers",
    url: "https://example.test/issues/90001",
    state: "admitted",
    task_id: taskId,
    task_paused: false,
    admitted_at: at,
    deadline_at: "2026-10-19T10:00:00Z",
    turns_used: 2,
    committed_cost_usd: 8,
    policy,
    limits: {},
    starts: [
      {
        start_key: `factory-node:${taskId}:implement:1`,
        model: "synthetic-worker",
        status: "succeeded",
        cost_usd: 1.25,
      },
    ],
    stop_events: [],
    evidence: {
      pr_url:
        "https://example.test/synthetic-organization/infrastructure-observation-and-coordination/pull/90001",
      reason: "Synthetic evidence remains attached to this receipt.",
    },
    nodes: [
      {
        node_key: "implement",
        label: "Inspect synthetic operator confirmation presentation",
        kind: "implement",
        model: "synthetic-worker",
        state: "running",
        deps: [],
        attempts: [
          {
            attempt: 1,
            status: "running",
            cost_usd: 1.25,
            session: {
              id: 80001,
              model: "synthetic-worker",
              status: "running",
              guest_bound: true,
              last_turn_at: at,
              result_head:
                "Synthetic inspection of a long receipt identifier and its dependent work-item relationships.",
            },
          },
        ],
      },
    ],
    ...overrides,
  };
}
export function escalations() {
  return [31, 32].map((id) => ({
    receipt_id: id,
    issue_number: 90000 + id,
    work_item_id: 100000 + id,
    title: `Synthetic ${id}: retain receipt-specific notes while an operator checks the boundary between two related work items`,
    url: `https://example.test/issues/${90000 + id}`,
    task_class: "bug-fix",
    kind: "delivery",
    state: "escalated",
    open: true,
    briefing: false,
    decision_id: `decision:${String(id).repeat(32)}`,
    recommendation: "continue-narrowed",
    summary:
      "The repository change is staged. Operational acceptance remains outstanding.",
    question: "Which synthetic scope should this receipt record?",
    branch: `factory/t-synthetic-receipt-${id}-operator-confirmation-boundary`,
    pr_url: `https://example.test/pull/${90000 + id}`,
    options: [
      {
        key: "continue-narrowed",
        label: "Deliver the narrowed repository scope",
        effect: "agent-ready",
        children: 0,
      },
      { key: "hold", label: "Leave it open", effect: "hold", children: 0 },
    ],
    escape: [
      { key: "escape:close", label: "Close the issue", effect: "escape-close" },
      { key: "escape:defer", label: "Defer it", effect: "escape-defer" },
      {
        key: "escape:dismiss",
        label: "Dismiss the escalation",
        effect: "escape-dismiss",
      },
    ],
    chat: [],
    resolved: null,
  }));
}
export function context(id) {
  return {
    receipt_id: id,
    ask: {
      line: `Synthetic receipt ${id}: inspect the operator boundary`,
      issue_number: 90000 + id,
      url: `https://example.test/issues/${90000 + id}`,
      body_head:
        "Long synthetic evidence keeps the confirmation pinned to the correct receipt.",
    },
    stopped: {
      line: "waiting for a synthetic operator decision",
      kind: "delivery",
      question: "What scope is safe?",
      summary: "Repository work only.",
    },
    happened: {
      line: "1 attempt, repository inspection complete",
      attempts: 1,
      branch: `factory/synthetic-${id}`,
    },
    cost: {
      line: "$1.25 of $36.00 across 1 task",
      committed: 1.25,
      ceiling: 36,
      num_tasks: 1,
    },
    lineage: {
      line: "blocked by synthetic work item 100099",
      blocked_by: [{ id: 100099, title: "Synthetic dependency" }],
      children: [],
      blocks: [],
      prior_receipts: [],
    },
  };
}
export function workItem(empty = false) {
  return {
    item: {
      id: 100001,
      github_issue_number: 90001,
      source_ref: "https://example.test/issues/90001",
      state: "ready",
      authority: "github",
      trust: "trusted",
      task_class: "bug-fix",
      labels: [
        "agent-ready",
        "synthetic-long-relationship-and-operator-boundary-label",
      ],
      title:
        "Synthetic work item: preserve relationship controls across long identifiers and operational evidence",
    },
    edges_in: empty
      ? []
      : [
          { id: 1, from_id: 100002, kind: "blocks", source: "github_body" },
          { id: 2, from_id: 100003, kind: "parent", source: "decision" },
          { id: 3, from_id: 100004, kind: "supersedes", source: "manual" },
        ],
    edges_out: empty
      ? []
      : [
          { id: 4, to_id: 100005, kind: "blocks", source: "manual" },
          { id: 5, to_id: 100006, kind: "parent", source: "decision" },
          { id: 6, to_id: 100007, kind: "supersedes", source: "manual" },
        ],
    receipts: empty
      ? []
      : [
          {
            id: 31,
            generation: 3,
            task_class: "bug-fix",
            state: "escalated",
            created_at: at,
          },
        ],
    events: empty
      ? []
      : [
          {
            op: "synthetic_relationship_observation",
            author_kind: "operator",
            author: "synthetic-operator@example.test",
            created_at: at,
            change_json: JSON.stringify({
              reason:
                "A long synthetic relationship observation is recorded without touching the factory.",
              task_id: taskId,
            }),
          },
        ],
  };
}
export function board(empty = false) {
  return {
    tracking_items: empty
      ? []
      : [
          trackingItem(),
          trackingItem({
            id: 100102,
            tracking_kind: "manual",
            state: "done",
            completion_evidence: "Synthetic owner supplied a completion note.",
          }),
        ],
    ok: true,
    state: "enabled",
    version: 44,
    policy,
    lanes: {
      delivery: { active: empty ? 0 : 1, limit: 1 },
      advisory: { active: 0, limit: 2 },
    },
    active: empty ? [] : [receipt()],
    queued: empty
      ? []
      : [
          receipt({
            id: 12,
            issue_number: 90002,
            task_id: null,
            state: "queued",
            nodes: [],
            starts: [],
          }),
        ],
    recent: empty
      ? []
      : [
          receipt({
            id: 10,
            task_id: "t-synthetic-finished",
            state: "failed",
            nodes: [],
            starts: [],
          }),
        ],
    escalations: empty ? [] : escalations(),
  };
}
export function fixtureData(view, scenario = "populated") {
  const error = scenario === "unavailable",
    empty = scenario === "empty";
  if (view === "board")
    return { board: error ? null : board(empty), task: null, error };
  if (view === "escalations")
    return { escalations: error || empty ? [] : escalations(), error };
  if (scenario === "missing") return { itemId, missing: true };
  if (scenario === "tracking")
    return {
      itemId,
      document: {
        item: trackingItem(),
        events: [
          {
            version: 1,
            op: "tracking_create",
            author_kind: "operator",
            author: "synthetic-operator@example.test",
            created_at: at,
            change_json: JSON.stringify({
              execution_mode: "tracking_only",
              state: "open",
            }),
          },
        ],
        receipts: [],
        edges_in: [],
        edges_out: [],
      },
      error: false,
    };
  return { itemId, document: error ? null : workItem(empty), error };
}

export function trackingItem(overrides = {}) {
  return {
    id: 100101,
    execution_mode: "tracking_only",
    tracking_kind: "external",
    title: "Synthetic external task with a long source and named owner",
    source_kind: "mcp",
    source_ref:
      "https://example.test/synthetic-external-owner/" +
      "long-reference-".repeat(15),
    owner: "Synthetic human and external coordination owner ".repeat(5),
    state: "active",
    blocker: "Waiting for the external owner to confirm their next step.",
    next_action: "Record the owner's evidence when it arrives.",
    tracking_version: 1,
    created_at: at,
    updated_at: at,
    completed_at: null,
    completion_evidence: null,
    ...overrides,
  };
}
