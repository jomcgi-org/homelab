import { describe, expect, it } from "vitest";
import {
  activityRow,
  activitySummary,
  attemptMark,
  attemptWord,
  clip,
  commitUrl,
  diffStat,
  duration,
  groupByDay,
  ledger,
  ledgerMeta,
  money,
  nodeWord,
  outcome,
  planStrip,
  plural,
  prettyCommand,
  relative,
  reviewRounds,
  sessionHref,
  sessionSpec,
  stampUtc,
  stepsOf,
  stopRows,
  stripRationale,
  taskMark,
  taskSpec,
  tokens,
  toolLabel,
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
  cost_usd: 3.42,
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
  cost_usd: 1.94,
  pr: { number: 5991, url: "https://example.test/pull/5991", state: "merged" },
  nodes: [],
  stop_events: [],
};

describe("formatting", () => {
  it("renders money to two places and tolerates a missing number", () => {
    expect(money(3.4)).toBe("$3.40");
    expect(money(null)).toBe("$0.00");
  });

  it("collapses millions of tokens to one decimal", () => {
    expect(tokens(10_762_310)).toBe("10.8M");
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
    queued: [{ issue_number: 5992, state: "queued", cost_usd: 0, nodes: [] }],
    recent: [
      { ...LANDED_TASK, finished_at: "2026-09-09T12:31:00Z" },
      LANDED_TASK,
      {
        issue_number: 5971,
        state: "failed",
        finished_at: "2026-09-09T21:40:00Z",
        cost_usd: 2.15,
        nodes: [],
      },
      {
        issue_number: 5000,
        state: "landed",
        finished_at: "2026-08-01T10:00:00Z",
        cost_usd: 9,
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

  it("reads the snapshot's seven-day totals over the capped recent list", () => {
    const result = ledger(
      {
        ...board,
        totals_7d: { landed: 20, escalated: 10, spend_usd: 34.04 },
      },
      NOW,
    );
    expect(result.landed).toBe(20);
    expect(result.escalated).toBe(10);
    expect(result.spend).toBeCloseTo(34.04, 5);
    expect(result.done).toHaveLength(4);
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
      "in flight · review_1 · 5h 42m left",
    );
  });

  it("says how many slots a queued task waits on", () => {
    expect(ledgerMeta({ state: "queued" }, POLICY, NOW)).toBe(
      "queued · 2 slots",
    );
  });

  it("names the merged PR and the round count", () => {
    expect(ledgerMeta(LANDED_TASK, POLICY, NOW)).toBe(
      "landed · PR #5991 merged · 0 review rounds",
    );
    expect(ledgerMeta({ ...LANDED_TASK, pr: null }, POLICY, NOW)).toBe(
      "landed · 0 review rounds",
    );
  });

  it("keeps a landing task's PR state", () => {
    expect(
      ledgerMeta(
        { ...LIVE_TASK, state: "landing", pr: { number: 7, state: "queued" } },
        POLICY,
        NOW,
      ),
    ).toBe("landing · PR #7 queued · 1 review round");
  });

  it("marks an escalation and a cancellation by their word alone", () => {
    expect(ledgerMeta({ state: "failed", review_rounds: 2 }, POLICY, NOW)).toBe(
      "escalated · 2 review rounds",
    );
    expect(ledgerMeta({ state: "cancelled" }, POLICY, NOW)).toBe(
      "cancelled · 0 review rounds",
    );
  });
});

describe("outcome", () => {
  const text = (verdict) => verdict.parts.map((part) => part.text).join("");

  it("reports a landed task with its PR link", () => {
    const verdict = outcome(LANDED_TASK, POLICY, NOW);
    expect(verdict.headline).toBe("landed");
    expect(verdict.tone).toBe("landed");
    expect(verdict.parts[1]).toEqual({
      text: "#5991",
      href: "https://example.test/pull/5991",
    });
    expect(text(verdict)).toBe("PR #5991 merged · 0 review rounds · 1h 39m");
  });

  it("points a live task at its running step and its draft PR", () => {
    const verdict = outcome(LIVE_TASK, POLICY, NOW, 4);
    expect(verdict.headline).toBe("in flight");
    expect(verdict.parts).toContainEqual({ text: "review_1", code: true });
    expect(verdict.parts).toContainEqual({ text: "step 5", step: 5 });
    expect(verdict.parts).toContainEqual({
      text: "#5996",
      href: "https://example.test/pull/5996",
    });
    expect(text(verdict)).toBe(
      "At review_1 (step 5) · PR #5996 draft · 7 of 12 starts · 5h 42m left",
    );
  });

  it("gives a landing task its PR state and the deadline", () => {
    const verdict = outcome({ ...LIVE_TASK, state: "landing" }, POLICY, NOW);
    expect(verdict.headline).toBe("landing");
    expect(verdict.mark).toBe("running live");
    expect(text(verdict)).toBe("PR #5996 draft · 1 review round · 5h 42m left");
  });

  it("leads an escalation with the evidence and says it waits without one", () => {
    const withReason = outcome(
      { ...LANDED_TASK, state: "failed", evidence_reason: "Budget gone" },
      POLICY,
      NOW,
    );
    expect(withReason.headline).toBe("escalated");
    expect(text(withReason)).toBe("Budget gone");
    const bare = outcome({ ...LANDED_TASK, state: "failed" }, POLICY, NOW);
    expect(text(bare)).toBe("Waits for a person.");
  });

  it("gives a cancelled task the record's own reason", () => {
    const verdict = outcome(
      {
        ...LANDED_TASK,
        state: "cancelled",
        stop_events: [{ reason: "operator_release" }],
      },
      POLICY,
      NOW,
    );
    expect(verdict.headline).toBe("cancelled");
    expect(text(verdict)).toBe("operator_release");
    expect(
      text(outcome({ ...LANDED_TASK, state: "cancelled" }, POLICY, NOW)),
    ).toBe("Cancelled by an operator.");
  });

  it("explains what a queued task waits on", () => {
    expect(text(outcome({ state: "queued" }, POLICY, NOW))).toBe(
      "Waits for a slot · 2 slots",
    );
  });

  it("names the phase an uncertain task is stuck at", () => {
    const verdict = outcome({ ...LIVE_TASK, state: "uncertain" }, POLICY, NOW);
    expect(verdict.tone).toBe("uncertain");
    expect(verdict.parts).toContainEqual({ text: "review_1", code: true });
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

const DIFF = [
  "diff --git a/one.py b/one.py",
  "--- a/one.py",
  "+++ b/one.py",
  "@@ -1,2 +1,3 @@",
  " keep",
  "-gone",
  "+new",
  "+also added",
  "diff --git a/two.py b/two.py",
  "--- a/two.py",
  "+++ b/two.py",
  "@@ -1 +1 @@",
  "-old",
  "+fresh",
].join("\n");

describe("diffStat", () => {
  it("counts files, additions and deletions without the file headers", () => {
    expect(diffStat(DIFF)).toEqual({ files: 2, additions: 3, deletions: 2 });
    expect(diffStat(null)).toBeNull();
  });
});

describe("planStrip", () => {
  it("lists every node in plan order with its attempts folded in", () => {
    const steps = stepsOf(LIVE_TASK);
    const strip = planStrip(LIVE_TASK, steps);
    expect(strip.map((entry) => entry.number)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(strip[0]).toMatchObject({
      step: 1,
      attempts: 1,
      cost: 0,
      deps: [],
    });
    expect(strip[1].deps).toEqual(["plan"]);
    // A node that has not run yet points at no step.
    expect(strip[5].step).toBeNull();
  });

  it("sums an attempt's cost into its node", () => {
    const task = {
      nodes: [
        {
          node_key: "a",
          attempts: [
            { attempt: 1, cost_usd: 1.25 },
            { attempt: 2, cost_usd: 0.5 },
          ],
        },
      ],
    };
    expect(planStrip(task, stepsOf(task))[0]).toMatchObject({
      attempts: 2,
      cost: 1.75,
    });
    expect(planStrip(null)).toEqual([]);
  });
});

describe("stopRows", () => {
  it("orders events newest first and splits their stamps", () => {
    const rows = stopRows({
      stop_events: [
        { at: "2026-09-11T08:00:00Z", action: "stop_settled", reason: null },
        {
          at: "2026-09-11T09:30:00Z",
          action: "stop_observation",
          reason: "lost_before_guest",
          intervention_required: true,
        },
      ],
    });
    expect(rows).toEqual([
      {
        day: "2026-09-11",
        clock: "09:30",
        action: "stop_observation",
        reason: "lost_before_guest",
        person: true,
      },
      {
        day: "2026-09-11",
        clock: "08:00",
        action: "stop_settled",
        reason: "",
        person: false,
      },
    ]);
    expect(stopRows(null)).toEqual([]);
  });
});

describe("stripRationale", () => {
  it("drops the trailer the record parsed out, and only that", () => {
    const raw = "RATIONALE\n- path: a.py · why: it";
    expect(stripRationale(`Done.\n\n${raw}`, { raw })).toBe("Done.");
    expect(stripRationale("Done.", { raw })).toBe("Done.");
    expect(stripRationale("Done.", null)).toBe("Done.");
    expect(stripRationale(null, null)).toBe("");
  });
});

describe("toolLabel", () => {
  it("puts an MCP tool's own name first and its server second", () => {
    expect(toolLabel("mcp__agents__search_knowledge")).toBe(
      "search_knowledge · agents",
    );
    expect(toolLabel("ToolSearch")).toBe("ToolSearch");
    expect(toolLabel(null)).toBe("tool");
  });
});

describe("activityRow", () => {
  it("points an edit at its file", () => {
    const row = activityRow({ type: "edit", file_path: "two.py" });
    expect(row.type).toBe("edit");
    expect(row.what).toBe("two.py");
    expect(row.path).toBe("two.py");
  });

  it("leaves a command with nothing to open", () => {
    expect(activityRow({ type: "bash", command: "ci" })).toEqual({
      type: "bash",
      what: "ci",
      short: "ci",
      path: null,
    });
  });

  it("labels a tool call as tool and names the tool in the value column", () => {
    expect(
      activityRow({ type: "tool_use", name: "Read", file_path: "one.py" }),
    ).toEqual({
      type: "tool",
      what: "Read one.py",
      short: "one.py",
      path: null,
    });
  });

  it("shortens a path to its last segment for the digest line", () => {
    const row = activityRow({
      type: "edit",
      file_path: "projects/monolith/ember/probes/deliver.py",
    });
    expect(row.short).toBe("deliver.py");
    expect(row.what).toBe("projects/monolith/ember/probes/deliver.py");
  });
});

describe("prettyCommand", () => {
  it("unwraps the shell wrapper and rejoins a quoted argv", () => {
    expect(prettyCommand(`/bin/sh -lc '"git" "remote" "-v"'`)).toBe(
      "git remote -v",
    );
  });

  it("unescapes the argv quotes a double-quoted wrapper carries", () => {
    expect(prettyCommand('/bin/sh -lc "\\"rg\\" \\"-n\\" \\"pattern\\""')).toBe(
      "rg -n pattern",
    );
  });

  it("leaves a command that was never wrapped alone", () => {
    expect(prettyCommand("ci")).toBe("ci");
    expect(prettyCommand("git status --short")).toBe("git status --short");
  });

  it("keeps a heredoc intact, dropping only the wrapper", () => {
    const inner = "cat <<'EOF' > a.txt\nline one\nline two\nEOF";
    expect(prettyCommand(`/bin/bash -lc '${inner}'`)).toBe(inner);
  });

  it("leaves a shell line with its own quoting as it found it", () => {
    const inner = `pwd && rg --files -g 'AGENTS.md' | sort`;
    expect(prettyCommand(`/bin/sh -lc "${inner}"`)).toBe(inner);
  });

  it("never throws on odd input", () => {
    expect(prettyCommand(null)).toBe("");
    expect(prettyCommand(undefined)).toBe("");
    expect(prettyCommand(42)).toBe("42");
    expect(prettyCommand(`/bin/sh -lc '"git" "unbalanced`)).toBe(
      `/bin/sh -lc '"git" "unbalanced`,
    );
  });
});

describe("activitySummary", () => {
  const ACTIVITIES = [
    { type: "edit", file_path: "projects/monolith/factory/engine.py" },
    { type: "write", file_path: "a/b/new.py" },
    { type: "bash", command: `/bin/sh -lc '"git" "remote" "-v"'` },
    { type: "tool_use", name: "Read", file_path: "docs/one.md" },
    { type: "bash", command: "ci" },
    { type: "tool_use", name: "search_knowledge" },
  ];

  it("counts the kinds in the vocabulary the rows use, in reading order", () => {
    expect(activitySummary(ACTIVITIES).counts).toEqual([
      { kind: "edit", count: 1 },
      { kind: "write", count: 1 },
      { kind: "command", count: 2 },
      { kind: "read", count: 1 },
      { kind: "tool call", count: 1 },
    ]);
  });

  it("shows the first few rows short and says how many are left", () => {
    const digest = activitySummary(ACTIVITIES);
    expect(digest.shown).toEqual([
      {
        type: "edit",
        text: "engine.py",
        title: "projects/monolith/factory/engine.py",
      },
      { type: "write", text: "new.py", title: "a/b/new.py" },
      { type: "bash", text: "git remote -v", title: "git remote -v" },
      { type: "tool", text: "one.md", title: "Read docs/one.md" },
    ]);
    expect(digest.hidden).toBe(2);
  });

  it("hides nothing when the turn is shorter than the limit", () => {
    const digest = activitySummary(ACTIVITIES.slice(0, 2));
    expect(digest.shown).toHaveLength(2);
    expect(digest.hidden).toBe(0);
  });

  it("takes an explicit limit", () => {
    expect(activitySummary(ACTIVITIES, 1).hidden).toBe(5);
  });

  it("flattens a multi-line command to one clipped line", () => {
    const command = `/bin/sh -lc 'pwd\n${"rg --files ".repeat(20)}'`;
    const [row] = activitySummary([{ type: "bash", command }]).shown;
    expect(row.text).toHaveLength(80);
    expect(row.text.endsWith("…")).toBe(true);
    expect(row.text).not.toContain("\n");
  });

  it("is empty for a turn that recorded nothing", () => {
    expect(activitySummary(null)).toEqual({
      counts: [],
      shown: [],
      hidden: 0,
    });
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
    const row = activityRow({ type: "tool_use", name: "read_skill" });
    expect(row.type).toBe("tool");
    expect(row.what).toBe("read_skill");
    expect(
      activityRow({ type: "tool_use", name: "mcp__agents__report_knowledge" })
        .what,
    ).toBe("report_knowledge · agents");
  });

  it("keeps a tool's path beside its name", () => {
    const row = activityRow({
      type: "tool_use",
      name: "Read",
      file_path: "a/b/c.py",
    });
    expect(row.what).toBe("Read a/b/c.py");
    expect(row.short).toBe("c.py");
  });

  it("says when a bash command was not recorded", () => {
    expect(activityRow({ type: "bash" }).what).toBe("(command not recorded)");
    expect(activityRow({ type: "bash", command: "ci" }).what).toBe("ci");
  });
});
