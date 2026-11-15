import { describe, expect, it } from "vitest";
import {
  summarizeBoard,
  summarizeFactoryReading,
  waitingDecisions,
} from "./factory-summary.js";

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
      decision_id: "decision:31",
      kind: "delivery",
      options: [
        {
          key: "continue-narrowed",
          label: "Deliver it",
          effect: "agent-ready",
        },
        { label: "no key, dropped" },
      ],
      escape: [{ key: "escape:close" }],
      open: true,
      extra: "not copied",
    },
    { receipt_id: 30, issue_number: 90030, title: "Settled", open: false },
  ],
};

describe("summarizeBoard", () => {
  it("matches the summary projection and rejects incomplete counts", () => {
    const reading = {
      ...board,
      active_count: 2,
      queued_count: 1,
      paused_count: 1,
    };
    expect(summarizeFactoryReading(reading)).toEqual(summarizeBoard(board));
    for (const bad of [undefined, -1, 1.5, "2", NaN]) {
      expect(
        summarizeFactoryReading({ ...reading, active_count: bad }),
      ).toBeNull();
    }
    expect(summarizeFactoryReading({ ...reading, paused_count: 3 })).toBeNull();
  });
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
        decision_id: "decision:31",
        issue_number: 90031,
        work_item_id: 100031,
        kind: "delivery",
        briefing: false,
        title: "Retain receipt notes",
        question: "Which scope?",
        summary: "",
        recommendation: "continue-narrowed",
        url: null,
        pr_url: null,
        options: [
          {
            key: "continue-narrowed",
            label: "Deliver it",
            effect: "agent-ready",
            closes: [],
            in_favour_of: null,
            children: 0,
          },
        ],
        escape: [
          {
            key: "escape:close",
            label: "escape:close",
            effect: null,
            closes: [],
            in_favour_of: null,
            children: 0,
          },
        ],
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

  it("prints a reason code as words", () => {
    expect(
      summarizeBoard({ ok: false, reason: "not_initialized" }).reason,
    ).toBe("not initialized");
    expect(summarizeBoard({ ok: false }).reason).toBe("not ready");
  });

  it("treats a missing escalation list as nothing waiting", () => {
    expect(waitingDecisions({})).toEqual([]);
    expect(waitingDecisions({ escalations: [{ open: false }] })).toEqual([]);
  });
});
