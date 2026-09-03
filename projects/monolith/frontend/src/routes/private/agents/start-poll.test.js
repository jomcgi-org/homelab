import { describe, expect, test } from "vitest";
import {
  START_POLL_INTERVAL_MS,
  advanceStartPoll,
  initialStartPoll,
  startPollDelay,
} from "./start-poll.js";

describe("launcher start polling", () => {
  test("begins classifying and preserves the composed task", () => {
    const composed = { prompt: "Fix it", model: "terra" };
    expect(initialStartPoll("task-1", composed)).toMatchObject({
      taskId: "task-1",
      composed,
      polls: 0,
      kind: "classifying",
      terminal: false,
    });
  });

  test("counts classifying polls and keeps polling", () => {
    const state = advanceStartPoll(initialStartPoll("task-1"), {
      kind: "classifying",
    });
    expect(state).toMatchObject({ polls: 1, terminal: false });
  });

  test("backs off after ten polls and caps the interval", () => {
    expect(startPollDelay(0)).toBe(START_POLL_INTERVAL_MS);
    expect(startPollDelay(9)).toBe(START_POLL_INTERVAL_MS);
    expect(startPollDelay(10)).toBe(2000);
    expect(startPollDelay(11)).toBe(4000);
    expect(startPollDelay(30)).toBe(8000);
  });

  test.each([
    ["session", { session_id: 7 }],
    ["run", { run_id: "wf-1" }],
    ["needs_input", { needs_input: { repo: true, branch: true } }],
    ["refused", { message: "Not allowed" }],
    ["error", { message: "Unavailable" }],
  ])("makes %s terminal", (kind, fields) => {
    const result = { kind, ...fields };
    expect(advanceStartPoll(initialStartPoll("task-1"), result)).toMatchObject({
      kind,
      terminal: true,
      result,
    });
  });

  test("turns malformed and unknown responses into errors", () => {
    expect(advanceStartPoll(initialStartPoll("task-1"), null)).toMatchObject({
      kind: "error",
      terminal: true,
    });
    expect(
      advanceStartPoll(initialStartPoll("task-1"), { kind: "mystery" }),
    ).toMatchObject({ kind: "error", terminal: true });
  });
});
