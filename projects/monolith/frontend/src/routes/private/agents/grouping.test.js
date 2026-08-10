import { describe, expect, it } from "vitest";
import {
  groupSessions,
  groupSummary,
  isGroupExpanded,
  shortWorkflowId,
} from "./grouping.js";

describe("shortWorkflowId", () => {
  it("distinguishes ids that share a prefix", () => {
    // Taking the head collapsed both of these to "swarm-sm" against live data.
    expect(shortWorkflowId("swarm-smoke-1")).not.toBe(
      shortWorkflowId("swarm-smoke-2"),
    );
  });

  it("returns a short id unchanged, with no ellipsis", () => {
    expect(shortWorkflowId("wf-1")).toBe("wf-1");
  });

  it("coerces a non-string id", () => {
    expect(shortWorkflowId(42)).toBe("42");
  });
});

describe("groupSessions", () => {
  it("keeps sessions without workflow ids flat", () => {
    const entries = groupSessions([
      { id: "one", workflow_id: null },
      { id: "two", workflow_id: undefined },
      { id: "three", workflow_id: "" },
    ]);

    expect(entries).toEqual([
      { kind: "session", session: { id: "one", workflow_id: null } },
      { kind: "session", session: { id: "two", workflow_id: undefined } },
      { kind: "session", session: { id: "three", workflow_id: "" } },
    ]);
  });

  it("keeps a workflow with one session flat", () => {
    const session = { id: "one", workflow_id: 42 };

    expect(groupSessions([session])).toEqual([{ kind: "session", session }]);
  });

  it("groups shared workflows at their first member position", () => {
    const first = { id: "first", workflow_id: "wf", status: "running" };
    const unrelated = { id: "unrelated", workflow_id: null };
    const second = {
      id: "second",
      workflow_id: "wf",
      status: "completed",
    };

    expect(groupSessions([first, unrelated, second])).toEqual([
      {
        kind: "group",
        workflowId: "wf",
        sessions: [first, second],
        counts: { running: 1, completed: 1 },
      },
      { kind: "session", session: unrelated },
    ]);
  });

  it("rolls up status classes", () => {
    expect(
      groupSessions([
        { id: "one", workflow_id: "wf", status: "running" },
        { id: "two", workflow_id: "wf", status: "running", pending_count: 1 },
        { id: "three", workflow_id: "wf", status: "needs_input" },
      ])[0],
    ).toMatchObject({ counts: { running: 1, working: 1, needs_input: 1 } });
  });
});

describe("groupSummary", () => {
  it("omits zero counts and orders running before completed", () => {
    expect(
      groupSummary({ completed: 1, running: 3, warn: 0, working: 0 }),
    ).toBe("3 running · 1 completed");
  });
});

describe("isGroupExpanded", () => {
  const recentGroup = {
    workflowId: "recent",
    sessions: [{ id: "selected" }, { id: "other" }],
  };

  it("lets an explicit false collapse a group containing the selection", () => {
    expect(
      isGroupExpanded(recentGroup, {
        active: false,
        selectedId: "selected",
        expanded: { recent: false },
      }),
    ).toBe(false);
  });

  it("lets an explicit true expand a collapsed Recent group", () => {
    expect(
      isGroupExpanded(
        { workflowId: "recent", sessions: [{ id: "other" }] },
        { active: false, selectedId: "selected", expanded: { recent: true } },
      ),
    ).toBe(true);
  });

  it("defaults Active groups expanded and Recent groups collapsed", () => {
    expect(
      isGroupExpanded(
        { workflowId: "active", sessions: [{ id: "other" }] },
        { active: true, selectedId: "selected", expanded: {} },
      ),
    ).toBe(true);
    expect(
      isGroupExpanded(
        { workflowId: "recent", sessions: [{ id: "other" }] },
        { active: false, selectedId: "selected", expanded: {} },
      ),
    ).toBe(false);
  });

  it("defaults a Recent group containing the selection expanded", () => {
    expect(
      isGroupExpanded(recentGroup, {
        active: false,
        selectedId: "selected",
        expanded: {},
      }),
    ).toBe(true);
  });
});
