import { describe, expect, it } from "vitest";
import { collisionWording, moveCountdown, progressSummary } from "./moving.js";

const tasks = [{ id: "task-1", title: "Ship the boxes", done_at: null }];
const spans = [
  { id: "span-1", kind: "visitor", label: "Visitors" },
  { id: "span-2", kind: "trip", label: "Japan trip" },
];

describe("collision wording", () => {
  it("words a span overlap using resolved labels and its date range", () => {
    expect(
      collisionWording(
        {
          type: "span_span",
          item1_id: "span-1",
          item2_id: "span-2",
          overlaps_from: "2026-05-03",
          overlaps_to: "2026-05-07",
        },
        tasks,
        spans,
      ),
    ).toBe("Visitors overlaps Japan trip, 3 to 7 May");
  });

  it("words a task due inside a span using resolved labels", () => {
    expect(
      collisionWording(
        {
          type: "task_span",
          item1_id: "task-1",
          item2_id: "span-1",
          overlaps_from: "2026-05-03",
          overlaps_to: "2026-05-03",
        },
        tasks,
        spans,
      ),
    ).toBe("Ship the boxes is due during Visitors");
  });
});

describe("move countdown", () => {
  it("targets the earliest move span", () => {
    const result = moveCountdown(
      [
        { kind: "move", starts_on: "2026-05-16" },
        { kind: "move", starts_on: "2026-05-11" },
        { kind: "trip", starts_on: "2026-05-02" },
      ],
      new Date(2026, 4, 1, 12),
    );

    expect(result.headline).toBe("10 days to go");
    expect(result.detail).toBe("Monday, 11 May 2026");
  });

  it("states plainly when no move span exists", () => {
    expect(moveCountdown([], new Date(2026, 4, 1, 12))).toEqual({
      headline: "No move date set",
      detail: "Add a move span when the date is known.",
    });
  });
});

describe("progress", () => {
  it("renders zero tasks without NaN or division by zero", () => {
    expect(progressSummary(0, [])).toEqual({
      done: 0,
      total: 0,
      value: 0,
      percent: 0,
      label: "0 of 0 done",
    });
  });
});
