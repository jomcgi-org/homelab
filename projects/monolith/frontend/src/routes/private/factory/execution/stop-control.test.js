import { describe, expect, test } from "vitest";
import {
  makeStopRequest,
  reconcileStop,
  stopInFlight,
} from "./stop-control.js";

const identity = { turn_seq: 4, dispatch_id: "dispatch-4" };

describe("Stop control state", () => {
  test("builds only the exact identity observed by the client", () => {
    expect(makeStopRequest(identity)).toEqual(identity);
    expect(makeStopRequest({ turn_seq: 4 })).toBe(null);
  });

  test("keeps requested pending while the same dispatch remains active", () => {
    const status = { ...identity, outcome: "requested" };
    expect(
      reconcileStop(status, {
        turns: [],
        stop_control: { active: identity },
      }),
    ).toEqual(status);
    expect(stopInFlight(status)).toBe(true);
  });

  test("confirms only a persisted user_interrupt terminal result", () => {
    expect(
      reconcileStop(
        { ...identity, outcome: "requested" },
        { turns: [{ seq: 4, terminal_reason: "user_interrupt" }] },
      ).outcome,
    ).toBe("confirmed");
  });

  test("reports completion winning the race without replacing its result", () => {
    const result = reconcileStop(
      { ...identity, outcome: "requested" },
      { turns: [{ seq: 4, terminal_reason: "completed" }] },
    );
    expect(result.outcome).toBe("completed");
    expect(result.terminal_reason).toBe("completed");
  });

  test("retains failed outcomes and makes a lost requested identity unknown", () => {
    const failed = { ...identity, outcome: "failed", reason: "stale" };
    expect(reconcileStop(failed, { turns: [] })).toEqual(failed);
    expect(
      reconcileStop(
        { ...identity, outcome: "requested" },
        { turns: [], stop_control: { active: null } },
      ).outcome,
    ).toBe("unknown");
  });
});
