import { describe, expect, it } from "vitest";
import {
  collisionWording,
  ganttDatePosition,
  mergeAgendaItems,
  moveCountdown,
  progressSummary,
  sumSellValues,
} from "./moving.js";

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
    expect(result.days).toBe(10);
  });

  it("states plainly when no move span exists", () => {
    expect(moveCountdown([], new Date(2026, 4, 1, 12))).toEqual({
      headline: "No move date set",
      detail: "Add a move span when the date is known.",
      days: null,
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
      label: "0 of 0 tasks",
    });
  });
});

describe("gantt positioning", () => {
  it("maps dates onto the timeline and clamps values outside it", () => {
    expect(ganttDatePosition("2026-08-01", "2026-08-01", "2026-08-11")).toBe(0);
    expect(ganttDatePosition("2026-08-06", "2026-08-01", "2026-08-11")).toBe(
      50,
    );
    expect(ganttDatePosition("2027-01-01", "2026-08-01", "2026-08-11")).toBe(
      100,
    );
  });

  it("rejects invalid or zero-length ranges", () => {
    expect(ganttDatePosition("bad", "2026-08-01", "2026-08-11")).toBeNull();
    expect(
      ganttDatePosition("2026-08-01", "2026-08-01", "2026-08-01"),
    ).toBeNull();
  });
});

describe("phone agenda", () => {
  it("merges spans, milestones, and collisions in date order", () => {
    const items = mergeAgendaItems(
      [
        {
          id: "milestone-1",
          title: "Movers booked",
          occurs_on: "2026-09-04",
          owner: "both",
          gcal_state: "held",
        },
      ],
      [
        {
          id: "span-1",
          kind: "visitor",
          label: "Visitors",
          starts_on: "2026-09-02",
          ends_on: "2026-09-05",
        },
      ],
      [
        {
          type: "task_span",
          item1_id: "task-1",
          item2_id: "span-1",
          overlaps_from: "2026-09-03",
          overlaps_to: "2026-09-03",
        },
      ],
      tasks,
    );

    expect(items.map((item) => item.kind)).toEqual(["span", "col", "ms"]);
    expect(items[1].icon).toBe("▲");
    expect(items[2]).toMatchObject({
      held: true,
      monthLabel: "September 2026",
    });
  });
});

describe("sell totals", () => {
  it("adds numeric CAD values only for sell tasks", () => {
    expect(
      sumSellValues([
        { track: "sell", value_cad: "125.50" },
        { track: "sell", value_cad: 24.5 },
        { track: "sell", value_cad: null },
        { track: "ship", value_cad: "900" },
      ]),
    ).toBe(150);
  });
});
