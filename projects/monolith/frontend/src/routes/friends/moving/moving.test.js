import { describe, expect, it } from "vitest";
import {
  collisionWording,
  ganttDatePosition,
  groupMilestonesByDate,
  mergeAgendaItems,
  moveCountdown,
  packLaneBars,
  packLegTags,
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
  it("targets the earliest move span, counting to its end date", () => {
    const result = moveCountdown(
      [
        { kind: "move", starts_on: "2026-05-16", ends_on: "2026-05-25" },
        { kind: "move", starts_on: "2026-05-11", ends_on: "2026-05-21" },
        { kind: "trip", starts_on: "2026-05-02", ends_on: "2026-05-05" },
      ],
      new Date(2026, 4, 1, 12),
    );

    expect(result.headline).toBe("20 days to go");
    expect(result.detail).toBe("Thursday, 21 May 2026");
    expect(result.description).toBe(
      "Until you leave Canada, Thursday, 21 May 2026",
    );
    expect(result.days).toBe(20);
  });

  it("falls back when the earliest move span has no usable end date", () => {
    const result = moveCountdown(
      [{ kind: "move", starts_on: "2026-05-11", ends_on: null }],
      new Date(2026, 4, 1, 12),
    );

    expect(result).toEqual({
      headline: "No move date set",
      detail: "Add a move span when the date is known.",
      description: null,
      days: null,
    });
  });

  it("states plainly when no move span exists", () => {
    expect(moveCountdown([], new Date(2026, 4, 1, 12))).toEqual({
      headline: "No move date set",
      detail: "Add a move span when the date is known.",
      description: null,
      days: null,
    });
  });
});

describe("lane bar packing", () => {
  it("packs the real three-visitor collision into two sub-rows", () => {
    // Mirrors the real data: "Pat & Veronica" (18-31 Aug) and "Catherine &
    // Ross" (28 Aug-13 Sep) overlap and must split into sub-rows; a third,
    // later visitor has room to reuse the first sub-row.
    const bars = [
      {
        id: "span-pat-veronica",
        label: "Pat & Veronica",
        position: 14.05,
        width: 10.74,
      },
      {
        id: "span-catherine-ross",
        label: "Catherine & Ross",
        position: 22.31,
        width: 13.23,
      },
      {
        id: "span-later-visitor",
        label: "The Chungs",
        position: 66.12,
        width: 5.78,
      },
    ];

    const result = packLaneBars(bars);

    expect(result.subRowCount).toBe(2);
    const subRowById = Object.fromEntries(
      result.bars.map((bar) => [bar.id, bar.subRow]),
    );
    expect(subRowById["span-pat-veronica"]).toBe(0);
    expect(subRowById["span-catherine-ross"]).toBe(1);
    expect(subRowById["span-later-visitor"]).toBe(0);
  });

  it("never places two overlapping bars in the same sub-row", () => {
    const { bars, subRowCount } = packLaneBars([
      { id: "a", position: 0, width: 20 },
      { id: "b", position: 10, width: 20 },
      { id: "c", position: 15, width: 5 },
    ]);

    for (let row = 0; row < subRowCount; row += 1) {
      const inRow = bars.filter((bar) => bar.subRow === row);
      for (let i = 0; i < inRow.length; i += 1) {
        for (let j = i + 1; j < inRow.length; j += 1) {
          const left = inRow[i];
          const right = inRow[j];
          const overlaps =
            left.position < right.position + right.width &&
            right.position < left.position + left.width;
          expect(overlaps).toBe(false);
        }
      }
    }
  });

  it("breaks position ties deterministically", () => {
    const bars = [
      { id: "a", position: 0, width: 10 },
      { id: "b", position: 0, width: 10 },
    ];

    expect(packLaneBars(bars)).toEqual(packLaneBars(bars));
    const { bars: packed } = packLaneBars(bars);
    expect(packed.find((bar) => bar.id === "a").subRow).toBe(0);
    expect(packed.find((bar) => bar.id === "b").subRow).toBe(1);
  });

  it("gives a single bar its own sub-row", () => {
    const { bars, subRowCount } = packLaneBars([
      { id: "only", position: 40, width: 10 },
    ]);

    expect(subRowCount).toBe(1);
    expect(bars).toEqual([{ id: "only", position: 40, width: 10, subRow: 0 }]);
  });

  it("handles an empty lane", () => {
    expect(packLaneBars([])).toEqual({ bars: [], subRowCount: 0 });
  });
});

describe("leg tag staggering", () => {
  it("staggers the real move and trip tags, which start close together", () => {
    // "Pack out and leave Vancouver" (1 Oct) and "Japan" (11 Oct) sit
    // roughly 8% apart on the real four-month axis.
    const { tags, staggerCount } = packLegTags([
      { id: "span-move", label: "Pack out and leave Vancouver", position: 50 },
      { id: "span-japan", label: "Japan", position: 58 },
    ]);

    expect(staggerCount).toBe(2);
    expect(tags.find((tag) => tag.id === "span-move").stagger).toBe(0);
    expect(tags.find((tag) => tag.id === "span-japan").stagger).toBe(1);
  });

  it("does not stagger a single tag", () => {
    const { tags, staggerCount } = packLegTags([
      { id: "only", label: "Only leg", position: 30 },
    ]);

    expect(staggerCount).toBe(1);
    expect(tags[0].stagger).toBe(0);
  });

  it("handles no tags", () => {
    expect(packLegTags([])).toEqual({ tags: [], staggerCount: 0 });
  });
});

describe("milestone grouping by date", () => {
  it("groups the real triple-milestone date into one marker", () => {
    const milestones = [
      {
        id: "ms-lease",
        title: "Lease ends",
        occurs_on: "2026-10-07",
        owner: "both",
        gcal_state: "synced",
      },
      {
        id: "ms-joe",
        title: "Joe last day of work",
        occurs_on: "2026-10-07",
        owner: "joe",
        gcal_state: "held",
      },
      {
        id: "ms-anna",
        title: "Anna last day of work",
        occurs_on: "2026-10-07",
        owner: "anna",
        gcal_state: "synced",
      },
    ];

    const groups = groupMilestonesByDate(milestones);

    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      occursOn: "2026-10-07",
      count: 3,
      state: "held",
    });
    expect(groups[0].milestones).toHaveLength(3);
  });

  it("marks a group synced only when every milestone on the date is synced", () => {
    const groups = groupMilestonesByDate([
      { id: "a", occurs_on: "2026-09-01", gcal_state: "synced" },
      { id: "b", occurs_on: "2026-09-01", gcal_state: "synced" },
    ]);

    expect(groups[0].state).toBe("synced");
  });

  it("marks a group queued when nothing is held but not everything is synced", () => {
    const groups = groupMilestonesByDate([
      { id: "a", occurs_on: "2026-09-01", gcal_state: "synced" },
      { id: "b", occurs_on: "2026-09-01", gcal_state: "queued" },
    ]);

    expect(groups[0].state).toBe("queued");
  });

  it("keeps distinct dates as separate groups, sorted", () => {
    const groups = groupMilestonesByDate([
      { id: "a", occurs_on: "2026-09-05", gcal_state: "synced" },
      { id: "b", occurs_on: "2026-09-01", gcal_state: "synced" },
    ]);

    expect(groups.map((group) => group.occursOn)).toEqual([
      "2026-09-01",
      "2026-09-05",
    ]);
  });

  it("handles no milestones", () => {
    expect(groupMilestonesByDate([])).toEqual([]);
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
