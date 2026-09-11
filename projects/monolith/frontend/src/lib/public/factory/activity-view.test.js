import { describe, expect, it } from "vitest";
import {
  activityRow,
  attemptMark,
  attemptWord,
  briefRuns,
  clip,
  commitUrl,
  diffLines,
  diffStat,
  duration,
  groupByDay,
  hunkFor,
  ledger,
  ledgerMeta,
  money,
  nodeWord,
  outcome,
  planLayout,
  plural,
  relative,
  reviewRounds,
  sessionHref,
  sessionSpec,
  stampUtc,
  stepsOf,
  taskMark,
  taskSpec,
  tokens,
  turnMeta,
} from "./activity-view.js";

const NOW = "2026-09-11T14:32:00Z";

const POLICY = {
  generation: 7,
  max_tasks: 2,
  conductor_model: "opus",
  worker_model: "sol",
  reviewer_model: "opus",
  max_task_turns_hard: 12,
  max_parallel_nodes: 1,
  task_budget_usd: 8,
  max_attempts: 2,
  max_review_rounds: 2,
};

const LIVE_TASK = {
  issue_number: 5980,
  review_rounds: 1,
  state: "in flight",
  phase: "review_1",
  title: "Worker-delivered probes leave guests parked",
  admitted_at: "2026-09-11T08:14:00Z",
  deadline_at: "2026-09-11T20:14:00Z",
  turns_used: 7,
  allowance_turns: 12,
  committed_cost_usd: 3.42,
  pr: { number: 5996, url: "https://example.test/pull/5996", state: "draft" },
  nodes: [
    {
      node_key: "plan",
      kind: "plan",
      state: "done",
      deps: [],
      attempts: [{ attempt: 1, status: "succeeded" }],
    },
    {
      node_key: "implement",
      kind: "implement",
      state: "done",
      deps: ["plan"],
      attempts: [{ attempt: 1, status: "succeeded" }],
    },
    {
      node_key: "review",
      kind: "review",
      state: "done",
      deps: ["implement"],
      attempts: [{ attempt: 1, status: "succeeded" }],
    },
    {
      node_key: "correct_1",
      kind: "correct",
      state: "done",
      deps: ["review"],
      attempts: [{ attempt: 1, status: "succeeded" }],
    },
    {
      node_key: "review_1",
      kind: "review",
      state: "running",
      deps: ["correct_1"],
      attempts: [{ attempt: 1, status: "admitted" }],
    },
    {
      node_key: "verify_delivery",
      kind: "verify",
      state: "pending",
      deps: ["review_1"],
      attempts: [],
    },
  ],
  stop_events: [],
};

const LANDED_TASK = {
  issue_number: 5988,
  state: "landed",
  phase: "done",
  admitted_at: "2026-09-10T15:02:00Z",
  finished_at: "2026-09-10T16:41:00Z",
  turns_used: 5,
  allowance_turns: 8,
  committed_cost_usd: 1.94,
  pr: { number: 5991, url: "https://example.test/pull/5991", state: "merged" },
  nodes: [],
  stop_events: [],
};

describe("formatting", () => {
  it("renders money to two places and tolerates a missing number", () => {
    expect(money(3.4)).toBe("$3.40");
    expect(money(null)).toBe("$0.00");
  });

  it("collapses thousands of tokens to one decimal", () => {
    expect(tokens(999)).toBe("999");
    expect(tokens(18_420)).toBe("18.4k");
  });

  it("pluralises on the count", () => {
    expect(plural(1, "review round")).toBe("1 review round");
    expect(plural(2, "review round")).toBe("2 review rounds");
  });

  it("stamps timestamps in UTC", () => {
    expect(stampUtc("2026-09-11T08:14:00Z")).toBe("2026-09-11 08:14 UTC");
    expect(stampUtc(null)).toBe("");
  });

  it("reads a past time as elapsed and a future one as remaining", () => {
    expect(relative("2026-09-11T08:14:00Z", NOW)).toBe("6h 18m ago");
    expect(relative("2026-09-11T20:14:00Z", NOW)).toBe("5h 42m left");
    expect(relative("2026-09-11T14:20:00Z", NOW)).toBe("12m ago");
    expect(relative(null, NOW)).toBe("");
  });

  it("measures a closed interval", () => {
    expect(duration("2026-09-10T15:02:00Z", "2026-09-10T16:41:00Z")).toBe(
      "1h 39m",
    );
    expect(duration("2026-09-10T15:02:00Z", null)).toBe("");
  });

  it("links a commit to the repo", () => {
    expect(commitUrl("a41f2c9")).toBe(
      "https://github.com/jomcgi-org/homelab/commit/a41f2c9",
    );
    expect(commitUrl(null)).toBeNull();
  });
});

describe("state vocabulary", () => {
  it.each([
    ["in flight", "running live"],
    ["landed", "landed"],
    ["failed", "failed"],
    ["cancelled", "cancelled"],
    ["queued", "queued"],
    ["uncertain", "uncertain"],
    ["nonsense", "queued"],
  ])("marks the task state %s as %s", (state, mark) => {
    expect(taskMark(state)).toBe(mark);
  });

  it("calls a pending node queued and leaves the rest alone", () => {
    expect(nodeWord("pending")).toBe("queued");
    expect(nodeWord("retired")).toBe("retired");
  });

  it("calls an admitted attempt running", () => {
    expect(attemptWord("admitted")).toBe("running");
    expect(attemptWord("succeeded")).toBe("succeeded");
    expect(attemptMark("admitted")).toBe("running");
    expect(attemptMark("succeeded")).toBe("done");
    expect(attemptMark("failed")).toBe("failed");
  });

  it("counts correction nodes as the review rounds", () => {
    expect(reviewRounds(LIVE_TASK)).toBe(1);
    expect(reviewRounds({ nodes: [] })).toBe(0);
  });

  it("flattens nodes and attempts into steps in run order", () => {
    const steps = stepsOf(LIVE_TASK);
    expect(steps).toHaveLength(5);
    expect(steps.map((step) => step.node.node_key)).toEqual([
      "plan",
      "implement",
      "review",
      "correct_1",
      "review_1",
    ]);
  });
});

describe("ledger", () => {
  const board = {
    active: [LIVE_TASK],
    queued: [
      { issue_number: 5992, state: "queued", committed_cost_usd: 0, nodes: [] },
    ],
    recent: [
      { ...LANDED_TASK, finished_at: "2026-09-09T12:31:00Z" },
      LANDED_TASK,
      {
        issue_number: 5971,
        state: "failed",
        finished_at: "2026-09-09T21:40:00Z",
        committed_cost_usd: 2.15,
        nodes: [],
      },
      {
        issue_number: 5000,
        state: "landed",
        finished_at: "2026-08-01T10:00:00Z",
        committed_cost_usd: 9,
        nodes: [],
      },
    ],
  };

  it("sorts completed tasks newest first", () => {
    expect(ledger(board, NOW).done.map((task) => task.issue_number)).toEqual([
      5988, 5971, 5988, 5000,
    ]);
  });

  it("counts only the last seven days in the strip", () => {
    const result = ledger(board, NOW);
    expect(result.landed).toBe(2);
    expect(result.escalated).toBe(1);
    // 1.94 + 1.94 + 2.15 in the window, plus 3.42 already committed live.
    expect(result.spend).toBeCloseTo(9.45, 5);
  });

  it("survives an empty payload", () => {
    expect(ledger({}, NOW)).toMatchObject({
      live: [],
      queued: [],
      done: [],
      landed: 0,
    });
  });

  it("groups completed tasks under one heading per day", () => {
    const days = groupByDay(ledger(board, NOW).done);
    expect(days.map((entry) => entry.day)).toEqual([
      "2026-09-10",
      "2026-09-09",
      "2026-08-01",
    ]);
    expect(days[1].tasks).toHaveLength(2);
  });
});

describe("ledgerMeta", () => {
  it("names the phase and the deadline for a live task", () => {
    expect(ledgerMeta(LIVE_TASK, POLICY, NOW)).toBe(
      "in flight · at review_1 · deadline 5h 42m left",
    );
  });

  it("says what a queued task is waiting for", () => {
    expect(ledgerMeta({ state: "queued", nodes: [] }, POLICY, NOW)).toBe(
      "queued · waits for a slot, 2 tasks at a time",
    );
  });

  it("names the merged PR and the round count", () => {
    expect(ledgerMeta(LANDED_TASK, POLICY, NOW)).toBe(
      "landed · PR #5991 merged · 0 review rounds",
    );
  });

  it("says an escalation waits for a person", () => {
    expect(ledgerMeta({ state: "failed", nodes: [] }, POLICY, NOW)).toBe(
      "escalated · waits for a person · 0 review rounds",
    );
  });
});

describe("outcome", () => {
  it("reports a landed task with its PR link", () => {
    const result = outcome(LANDED_TASK, POLICY, NOW);
    expect(result).toMatchObject({ tone: "landed", headline: "landed" });
    expect(result.parts[1]).toEqual({
      text: "#5991",
      href: "https://example.test/pull/5991",
    });
    expect(result.parts[2].text).toContain("1h 39m from admission");
  });

  it("points a live task at its running step and its draft PR", () => {
    const result = outcome(LIVE_TASK, POLICY, NOW, 4);
    expect(result.headline).toBe("in flight");
    expect(result.parts.find((part) => part.code)).toEqual({
      text: "review_1",
      code: true,
    });
    expect(result.parts.find((part) => part.step)).toEqual({
      text: "step 5",
      step: 5,
    });
    expect(result.parts.at(-1).text).toContain("7 of 12 starts used");
  });

  it("leads an escalation with the evidence and drops the gap without one", () => {
    const withReason = outcome(
      {
        state: "failed",
        evidence_reason: "guest egress returned 422",
        nodes: [],
      },
      POLICY,
      NOW,
    );
    expect(withReason.parts[0].text).toMatch(
      /^guest egress returned 422\. The lane did not retry/,
    );
    const without = outcome({ state: "failed", nodes: [] }, POLICY, NOW);
    expect(without.parts[0].text).toMatch(/^The lane did not retry/);
  });

  it("gives a cancelled task the operator's reason", () => {
    const result = outcome(
      {
        state: "cancelled",
        nodes: [],
        stop_events: [{ reason: "superseded by #5485" }],
      },
      POLICY,
      NOW,
    );
    expect(result).toMatchObject({ tone: "cancelled", headline: "cancelled" });
    expect(result.parts[0].text).toBe("superseded by #5485");
  });

  it("explains what a queued task waits on", () => {
    const result = outcome({ state: "queued", nodes: [] }, POLICY, NOW);
    expect(result.parts[0].text).toContain("runs 2 tasks at a time");
  });
});

describe("specs", () => {
  it("reads elapsed time as so-far while a task is still running", () => {
    const rows = taskSpec(LIVE_TASK, POLICY, NOW);
    expect(rows.find((row) => row.label === "Elapsed").value).toBe(
      "6h 18m so far",
    );
    expect(rows.find((row) => row.label === "Spend").value).toBe(
      "$3.42 of $8.00",
    );
    expect(rows.find((row) => row.label === "Rounds").value).toBe("1 of 2");
  });

  it("closes elapsed time once a task has finished", () => {
    expect(
      taskSpec(LANDED_TASK, POLICY, NOW).find((row) => row.label === "Elapsed")
        .value,
    ).toBe("1h 39m");
  });

  it("says a still-running session has not ended", () => {
    const rows = sessionSpec({
      key: "factory:5980:implement:1",
      model: "sol",
      status: "admitted",
      turn_count: 2,
      cost_usd: 0.59,
      guest_bound: true,
      created_at: "2026-09-11T08:31:00Z",
      last_turn_at: null,
      terminal_reason: null,
      attempt: 1,
    });
    expect(rows.find((row) => row.label === "Ended").value).toBe(
      "still running",
    );
    expect(rows.find((row) => row.label === "Last turn").value).toBe("–");
    expect(rows.find((row) => row.label === "Guest").value).toBe("bound");
    expect(rows).toHaveLength(9);
  });
});

describe("planLayout", () => {
  it("puts each node one stage past its deepest dependency", () => {
    const steps = stepsOf(LIVE_TASK);
    const layout = planLayout(LIVE_TASK.nodes, steps);
    expect(layout.stages).toBe(6);
    expect(layout.nodes.map((box) => box.x)).toEqual([
      16, 210, 404, 598, 792, 986,
    ]);
    expect(layout.nodes.every((box) => box.y === 16)).toBe(true);
    expect(layout.width).toBe(1152);
    expect(layout.height).toBe(76);
  });

  it("stacks siblings that share a stage", () => {
    const nodes = [
      { node_key: "plan", deps: [], state: "done", attempts: [] },
      { node_key: "a", deps: ["plan"], state: "done", attempts: [] },
      { node_key: "b", deps: ["plan"], state: "done", attempts: [] },
    ];
    const layout = planLayout(nodes);
    expect(layout.stages).toBe(2);
    expect(layout.nodes[1].y).toBe(16);
    expect(layout.nodes[2].y).toBe(76);
  });

  it("draws one edge per dependency and dashes the ones out of a failed node", () => {
    const nodes = [
      { node_key: "plan", deps: [], state: "failed", attempts: [] },
      { node_key: "implement", deps: ["plan"], state: "pending", attempts: [] },
    ];
    const layout = planLayout(nodes);
    expect(layout.edges).toHaveLength(1);
    expect(layout.edges[0].dead).toBe(true);
    expect(layout.edges[0].d).toBe("M166 38 H188 V38 H205");
  });

  it("points each box at the first step that ran it", () => {
    const steps = stepsOf(LIVE_TASK);
    const layout = planLayout(LIVE_TASK.nodes, steps);
    expect(layout.nodes.map((box) => box.step)).toEqual([0, 1, 2, 3, 4, -1]);
  });

  it("returns an empty figure for an unplanned task", () => {
    expect(planLayout([])).toEqual({
      nodes: [],
      edges: [],
      width: 0,
      height: 0,
      stages: 0,
    });
  });

  it("widens every box to the longest node key in the plan", () => {
    const nodes = [
      { node_key: "conductor_1", deps: [], state: "done", attempts: [] },
      {
        node_key: "implement_fix_probe_worker_destroy",
        deps: ["conductor_1"],
        state: "done",
        attempts: [],
      },
    ];
    const layout = planLayout(nodes);
    expect(layout.nodes.map((box) => box.width)).toEqual([245, 245]);
    expect(layout.nodes[1].label).toBe("implement_fix_probe_worker_destroy");
    expect(layout.nodes[1].x).toBe(305);
    expect(layout.width).toBe(566);
  });

  it("truncates a key too long for the widest box and keeps the key itself", () => {
    const key = "implement".repeat(7);
    const layout = planLayout([
      { node_key: key, deps: [], state: "done", attempts: [] },
    ]);
    expect(key).toHaveLength(63);
    expect(layout.nodes[0].width).toBe(300);
    expect(layout.nodes[0].label).toHaveLength(42);
    expect(layout.nodes[0].label.endsWith("\u2026")).toBe(true);
    expect(layout.nodes[0].node.node_key).toBe(key);
  });

  it("does not recurse forever on a dependency cycle", () => {
    const nodes = [
      { node_key: "a", deps: ["b"], state: "done", attempts: [] },
      { node_key: "b", deps: ["a"], state: "done", attempts: [] },
    ];
    expect(() => planLayout(nodes)).not.toThrow();
  });
});

describe("clip", () => {
  it("leaves short text alone", () => {
    expect(clip("a short prompt")).toEqual({
      head: "a short prompt",
      rest: "",
      clipped: false,
    });
  });

  it("does not clip text of exactly the limit", () => {
    const text = "x".repeat(600);
    expect(clip(text).clipped).toBe(false);
    expect(clip("abcde", 5).clipped).toBe(false);
  });

  it("cuts on the last whitespace before the limit", () => {
    const text = `${"word ".repeat(200)}tail`;
    const cut = clip(text);
    expect(cut.clipped).toBe(true);
    expect(cut.head.length).toBeLessThanOrEqual(600);
    expect(cut.head.endsWith("word")).toBe(true);
    expect(cut.head + cut.rest).toBe(text);
  });

  it("cuts at the limit when one token runs past it", () => {
    const text = "x".repeat(900);
    const cut = clip(text);
    expect(cut.head).toHaveLength(600);
    expect(cut.rest).toHaveLength(300);
  });

  it("takes a limit and tolerates no text at all", () => {
    // Seven characters reach into "two", so the cut falls back to the
    // whitespace before it rather than splitting the word.
    expect(clip("one two three", 7)).toEqual({
      head: "one",
      rest: " two three",
      clipped: true,
    });
    expect(clip(null)).toEqual({ head: "", rest: "", clipped: false });
  });
});

describe("briefRuns", () => {
  it("turns a markdown heading paragraph into one heading run", () => {
    expect(briefRuns("## What happened")).toEqual([
      { text: "What happened", heading: true },
    ]);
    expect(briefRuns("###### Fix")).toEqual([{ text: "Fix", heading: true }]);
  });

  it("marks inline code spans", () => {
    expect(
      briefRuns("parked sessions count toward `session.maxSessions`"),
    ).toEqual([
      { text: "parked sessions count toward " },
      { text: "session.maxSessions", code: true },
    ]);
  });

  it("marks bold spans", () => {
    expect(briefRuns("left their guests **parked** with no id")).toEqual([
      { text: "left their guests " },
      { text: "parked", strong: true },
      { text: " with no id" },
    ]);
  });

  it("leaves every other markdown as plain text", () => {
    expect(briefRuns("a [link](x) and *one star* and # not a heading")).toEqual(
      [{ text: "a [link](x) and *one star* and # not a heading" }],
    );
  });

  it("returns nothing for an empty paragraph", () => {
    expect(briefRuns("")).toEqual([]);
    expect(briefRuns(null)).toEqual([]);
  });
});

const DIFF = `diff --git a/one.py b/one.py
--- a/one.py
+++ b/one.py
@@ -1,2 +1,3 @@
 keep
-gone
+added
+also added
diff --git a/two.py b/two.py
--- a/two.py
+++ b/two.py
@@ -1 +1 @@
-old
+new
`;

describe("diffs", () => {
  it("counts files, additions and deletions without the file headers", () => {
    expect(diffStat(DIFF)).toEqual({ files: 2, additions: 3, deletions: 2 });
    expect(diffStat(null)).toBeNull();
  });

  it("slices out one file's hunk by its post-image path", () => {
    expect(hunkFor(DIFF, "two.py")).toContain("-old");
    expect(hunkFor(DIFF, "two.py")).not.toContain("also added");
    expect(hunkFor(DIFF, "missing.py")).toBeNull();
    expect(hunkFor(null, "two.py")).toBeNull();
  });

  it("classifies lines as rows rather than markup", () => {
    const rows = diffLines(DIFF);
    expect(rows[0]).toEqual({
      cls: "fn",
      text: "diff --git a/one.py b/one.py",
    });
    expect(rows[1].cls).toBe("hd");
    expect(rows[3].cls).toBe("hd");
    expect(rows[4]).toEqual({ cls: "", text: " keep" });
    expect(rows[5].cls).toBe("del");
    expect(rows[6].cls).toBe("add");
    expect(diffLines(null)).toEqual([]);
  });
});

describe("activityRow", () => {
  it("opens an edit on the file's hunk", () => {
    const row = activityRow({ type: "edit", file_path: "two.py" }, DIFF);
    expect(row.type).toBe("edit");
    expect(row.what).toBe("two.py");
    expect(row.hunk).toContain("-old");
  });

  it("leaves a command with nothing to open", () => {
    expect(activityRow({ type: "bash", command: "ci" }, DIFF)).toEqual({
      type: "bash",
      what: "ci",
      short: "ci",
      hunk: null,
    });
  });

  it("labels a tool call as tool and names the tool in the value column", () => {
    expect(
      activityRow(
        { type: "tool_use", name: "Read", file_path: "one.py" },
        DIFF,
      ),
    ).toEqual({
      type: "tool",
      what: "Read one.py",
      short: "one.py",
      hunk: null,
    });
  });

  it("shortens a path to its last segment for the digest line", () => {
    const row = activityRow(
      { type: "edit", file_path: "projects/monolith/ember/probes/deliver.py" },
      DIFF,
    );
    expect(row.short).toBe("deliver.py");
    expect(row.what).toBe("projects/monolith/ember/probes/deliver.py");
  });

  it("gives an edit outside the diff nothing to open", () => {
    expect(
      activityRow({ type: "edit", file_path: "elsewhere.py" }, DIFF).hunk,
    ).toBeNull();
  });
});

describe("turnMeta", () => {
  it("reports cost, usage, the commit and how the turn stopped", () => {
    const parts = turnMeta({
      cost_usd: 0.58,
      usage: {
        input_tokens: 18_420,
        output_tokens: 2210,
        cache_read_tokens: 61_200,
      },
      commit_sha: "a41f2c9",
      base_sha: "3f1a0c2",
      stop_reason: "end_turn",
      terminal_reason: "completed",
      permission_denials: [],
    });
    expect(parts[0]).toEqual({ text: "$0.58" });
    expect(parts[1].text).toBe("18.4k in · 2.2k out · 61.2k cached");
    expect(parts[2]).toEqual({
      text: "commit ",
      sha: "a41f2c9",
      baseSha: "3f1a0c2",
    });
    expect(parts[3]).toEqual({ text: "stop ", strong: "end_turn" });
    expect(parts.some((part) => part.bad)).toBe(false);
  });

  it("carries the diff stat and says when the diff was clipped", () => {
    const parts = turnMeta({ cost_usd: 0.1, diff: DIFF, diff_truncated: true });
    expect(parts).toContainEqual({
      text: "",
      stat: { files: 2, additions: 3, deletions: 2 },
    });
    expect(parts).toContainEqual({ text: "diff truncated", bad: true });
  });

  it("flags a terminal reason that is not completion, and denials", () => {
    const parts = turnMeta({
      cost_usd: 0.64,
      terminal_reason: "network_op_in_flight",
      permission_denials: ["kubectl apply"],
    });
    expect(parts).toContainEqual({ text: "network_op_in_flight", bad: true });
    expect(parts).toContainEqual({ text: "1 permission denial", bad: true });
  });
});

describe("sessionHref", () => {
  it("keeps a node key with colons inside one path segment", () => {
    expect(sessionHref(5980, "verify:delivery", 1)).toBe(
      "/slop/factory/activity/5980/verify%3Adelivery/1",
    );
  });
});

describe("activityRow labels", () => {
  it("labels a tool row as tool and names the tool once, in the value column", () => {
    const row = activityRow({ type: "tool_use", name: "read_skill" }, null);
    expect(row.type).toBe("tool");
    expect(row.what).toBe("read_skill");
  });

  it("keeps a tool's path beside its name", () => {
    const row = activityRow(
      { type: "tool_use", name: "Read", file_path: "a/b/c.py" },
      null,
    );
    expect(row.what).toBe("Read a/b/c.py");
    expect(row.short).toBe("c.py");
  });

  it("says when a bash command was not recorded", () => {
    expect(activityRow({ type: "bash" }, null).what).toBe(
      "(command not recorded)",
    );
    expect(activityRow({ type: "bash", command: "ci" }, null).what).toBe("ci");
  });
});
