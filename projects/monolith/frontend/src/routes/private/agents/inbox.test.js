import { describe, expect, test } from "vitest";
import { arrivalSelection, inboxGroups } from "./inbox.js";

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
