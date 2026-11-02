import { describe, expect, it } from "vitest";
import { summarizeBoard, waitingDecisions } from "./factory-summary.js";

const board = {
  ok: true,
  state: "enabled",
  policy: { conductor_model: "astra-test" },
  active: [
    { id: 1, task_paused: false },
    { id: 2, task_paused: true },
  ],
  queued: [{ id: 3 }],
  escalations: [
    {
      receipt_id: 31,
      issue_number: 90031,
      work_item_id: 100031,
      title: "Retain receipt notes",
      question: "Which scope?",
      recommendation: "continue-narrowed",
      open: true,
      extra: "not copied",
    },
    { receipt_id: 30, issue_number: 90030, title: "Settled", open: false },
  ],
};

describe("summarizeBoard", () => {
  it("keeps the lane counts, open decisions and the conductor link", () => {
    const summary = summarizeBoard(board);
    expect(summary).toMatchObject({
      ok: true,
      reason: null,
      state: "enabled",
      inFlight: 2,
      queued: 1,
      paused: 1,
      conductor: { model: "astra-test" },
    });
    expect(summary.conductor.href).toMatch(
      /^\/factory\/execution\?compose=1&model=astra-test&prompt=/,
    );
    expect(summary.waiting).toEqual([
      {
        receipt_id: 31,
        issue_number: 90031,
        work_item_id: 100031,
        title: "Retain receipt notes",
        question: "Which scope?",
        recommendation: "continue-narrowed",
      },
    ]);
  });

  it("is null without a board and says why a board is not ready", () => {
    expect(summarizeBoard(null)).toBeNull();
    expect(summarizeBoard(undefined)).toBeNull();
    expect(summarizeBoard({ ok: false, reason: "paused" })).toMatchObject({
      ok: false,
      reason: "paused",
      state: "unknown",
      inFlight: 0,
      queued: 0,
      waiting: [],
    });
  });

  it("treats a missing escalation list as nothing waiting", () => {
    expect(waitingDecisions({})).toEqual([]);
    expect(waitingDecisions({ escalations: [{ open: false }] })).toEqual([]);
  });
});
