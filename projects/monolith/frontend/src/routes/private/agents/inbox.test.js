import { describe, expect, test } from "vitest";
import {
  arrivalSelection,
  inboxGroups,
  jumpTotal,
  railState,
  recentSummary,
} from "./inbox.js";

const run = (id, updatedAt, extra = {}) => ({
  workflow_id: id,
  dbos_status: "PENDING",
  updated_at: updatedAt,
  ...extra,
});

const session = (id, lastTurnAt, extra = {}) => ({
  id,
  status: "running",
  last_turn_at: lastTurnAt,
  ...extra,
});

describe("inbox groups", () => {
  test("interleaves needs-you runs and sessions by activity", () => {
    const groups = inboxGroups(
      [run("run-older", "2026-08-21T10:00:00Z", { needs: {} })],
      [
        session("session-newer", "2026-08-21T11:00:00Z", {
          status: "needs_input",
        }),
      ],
      {},
    );

    expect(groups.needsYou.map(({ kind, id }) => `${kind}:${id}`)).toEqual([
      "session:session-newer",
      "run:run-older",
    ]);
  });

  test("sorts running runs and sessions newest first", () => {
    const groups = inboxGroups(
      [run("run-middle", "2026-08-21T11:00:00Z")],
      [
        session("session-old", "2026-08-21T10:00:00Z"),
        session("session-new", "2026-08-21T12:00:00Z"),
      ],
      {},
    );

    expect(groups.running.map(({ kind, id }) => `${kind}:${id}`)).toEqual([
      "session:session-new",
      "run:run-middle",
      "session:session-old",
    ]);
  });

  test("keeps attention rows out of running and omits run-owned sessions", () => {
    const needsRun = run("run-needs", "2026-08-21T12:00:00Z", {
      needs: { kind: "human" },
    });
    const groups = inboxGroups(
      [needsRun],
      [
        session("run-session", "2026-08-21T13:00:00Z", {
          workflow_id: "run-needs",
        }),
      ],
      {},
    );

    expect(groups.needsYou.map(({ id }) => id)).toEqual(["run-needs"]);
    expect(groups.running).toEqual([]);
  });
});

describe("arrival selection", () => {
  test("uses the first needs-you run", () => {
    expect(
      arrivalSelection(
        [{ kind: "run", id: "run-needs" }],
        [{ kind: "session", id: "session-running" }],
      ),
    ).toEqual({ kind: "run", id: "run-needs" });
  });

  test("uses a needs-you session when no run needs attention", () => {
    expect(
      arrivalSelection(
        [{ kind: "session", id: "session-needs" }],
        [{ kind: "run", id: "run-running" }],
      ),
    ).toEqual({ kind: "session", id: "session-needs" });
  });

  test("falls back to the first running row", () => {
    expect(arrivalSelection([], [{ kind: "run", id: "run-running" }])).toEqual({
      kind: "run",
      id: "run-running",
    });
  });

  test("returns null when the inbox is empty", () => {
    expect(arrivalSelection([], [])).toBeNull();
  });
});

describe("rail state", () => {
  test.each([
    [{ needsYou: 0, running: 0, manual: null }, "folded"],
    [{ needsYou: 1, running: 0, manual: null }, "open"],
    [{ needsYou: 0, running: 1, manual: null }, "open"],
    [{ needsYou: 1, running: 1, manual: null }, "open"],
  ])("maps inbox transition %o to %s", (input, expected) => {
    expect(railState(input)).toBe(expected);
  });

  test("manual override wins in either inbox state", () => {
    expect(railState({ needsYou: 3, running: 2, manual: "folded" })).toBe(
      "folded",
    );
    expect(railState({ needsYou: 0, running: 0, manual: "open" })).toBe("open");
  });
});

describe("jump total", () => {
  test("counts every standalone session and every active or terminal run", () => {
    expect(
      jumpTotal(
        [
          session("active", "2026-08-22T12:00:00Z"),
          session("completed", "2026-08-21T12:00:00Z", {
            status: "completed",
          }),
          session("run-owned", "2026-08-22T11:00:00Z", {
            workflow_id: "active-run",
          }),
        ],
        [run("active-run", "2026-08-22T11:00:00Z")],
        [
          run("completed-run", "2026-08-21T11:00:00Z"),
          run("failed-run", "2026-08-20T11:00:00Z"),
        ],
      ),
    ).toBe(5);
  });
});

describe("recent summary", () => {
  test("interleaves five recent standalone sessions and runs with totals", () => {
    const now = new Date("2026-08-22T12:00:00Z");
    const summary = recentSummary(
      [
        session("session-new", "2026-08-22T11:00:00Z", {
          status: "completed",
          total_cost_usd: 0.03,
        }),
        session("run-owned", "2026-08-22T11:30:00Z", {
          workflow_id: "run-new",
          total_cost_usd: 9,
        }),
        session("session-old", "2026-08-14T11:00:00Z", {
          status: "completed",
          total_cost_usd: 7,
        }),
      ],
      [
        run("run-new", "2026-08-22T11:30:00Z", {
          dbos_status: "SUCCESS",
          cost_usd: 0.07,
        }),
        run("run-middle", "2026-08-22T10:30:00Z", {
          dbos_status: "SUCCESS",
          cost_usd: 0.02,
        }),
        run("run-four", "2026-08-22T10:00:00Z", {
          dbos_status: "SUCCESS",
          cost_usd: 0.01,
        }),
        run("run-five", "2026-08-22T09:30:00Z", {
          dbos_status: "SUCCESS",
          cost_usd: 0.04,
        }),
        run("run-six", "2026-08-22T09:00:00Z", {
          dbos_status: "SUCCESS",
          cost_usd: 0.05,
        }),
      ],
      now,
    );

    expect(summary.items.map(({ kind, id }) => `${kind}:${id}`)).toEqual([
      "run:run-new",
      "session:session-new",
      "run:run-middle",
      "run:run-four",
      "run:run-five",
    ]);
    expect(summary.count).toBe(6);
    expect(summary.allCount).toBe(7);
    expect(summary.sessionCount).toBe(1);
    expect(summary.runCount).toBe(5);
    expect(summary.spend).toBeCloseTo(0.22);
  });
});
