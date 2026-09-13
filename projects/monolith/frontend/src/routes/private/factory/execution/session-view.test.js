import { describe, expect, test } from "vitest";
import {
  defaultSessionView,
  incrementalAfterSeq,
  mergeTurns,
  SESSION_VIEW_CONVERSATION,
  SESSION_VIEW_WALKTHROUGH,
  turnHasWalkthrough,
  walkthroughTurns,
} from "./session-view.js";

test("incremental polling refetches and replaces an interrupted tail", () => {
  const interrupted = {
    seq: 2,
    terminal_reason: "interrupted",
    result_text: "Resuming after preemption",
  };
  const completed = {
    seq: 2,
    terminal_reason: "completed",
    result_text: "done",
  };

  expect(incrementalAfterSeq([{ seq: 1 }, interrupted])).toBe(1);
  expect(
    mergeTurns([{ seq: 1 }, interrupted], [completed, { seq: 3 }]),
  ).toEqual([{ seq: 1 }, completed, { seq: 3 }]);
});

const walkthroughTurn = {
  seq: 2,
  rationale: { parse_status: "parsed", paths: [{ path: "app.py" }] },
};

describe("session view", () => {
  test("a sleeping session with a walkthrough opens on the walkthrough", () => {
    expect(defaultSessionView("asleep", [walkthroughTurn])).toBe(
      SESSION_VIEW_WALKTHROUGH,
    );
  });

  test("an awake session with a walkthrough opens on the conversation", () => {
    expect(defaultSessionView("awake", [walkthroughTurn])).toBe(
      SESSION_VIEW_CONVERSATION,
    );
  });

  test("a sleeping session without a walkthrough opens on the conversation", () => {
    expect(
      defaultSessionView("asleep", [
        { seq: 1, rationale: { parse_status: "none" }, usage: {} },
      ]),
    ).toBe(SESSION_VIEW_CONVERSATION);
  });

  test("walkthrough turns retain session order and all composer inputs count", () => {
    const turns = [
      walkthroughTurn,
      { seq: 3, rationale: { parse_status: "none" } },
      { seq: 4, base_sha: "base", commit_sha: "head" },
      { seq: 5, usage: { activities: [{ type: "write", path: "b.py" }] } },
    ];

    expect(turns.map(turnHasWalkthrough)).toEqual([true, false, true, true]);
    expect(walkthroughTurns(turns).map((turn) => turn.seq)).toEqual([2, 4, 5]);
  });
});
