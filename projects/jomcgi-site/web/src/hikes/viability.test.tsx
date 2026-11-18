import { describe, expect, it } from "vitest";
import {
  doableDays,
  longestGoodRun,
  upcomingDays,
  viableDays,
  type Hour,
} from "./viability";

const base = Date.parse("2026-06-15T06:00:00Z") / 1000;
const hours = (offsets: number[]) => offsets.map((n) => base + n * 3600);
const rows = (offsets: number[]): Hour[] =>
  hours(offsets).map((ts) => [ts, 12, 0, 14, 40, null, null]);

describe("Python router.py parity (cases copied from hikes/router_test.py)", () => {
  // Test names, inputs and expected outputs come from the hub tests. Keep
  // the labels so a reviewer can compare each row with its Python source.
  it.each<[string, number[], number]>([
    ["test_empty", [], 0],
    ["test_single", [100], 1],
    ["test_contiguous", [100, 101, 102, 103], 4],
    ["test_one_gap_is_bridged", [100, 101, 103, 104], 4],
    ["test_second_gap_slides_the_run", [100, 102, 104], 2],
    ["test_two_hour_gap_is_not_bridged", [100, 101, 104, 105], 2],
    ["test_best_run_wins_after_break", [100, 101, 103, 104, 107, 108], 4],
  ])("TestLongestGoodRun.%s", (_name, indices, want) =>
    expect(longestGoodRun(indices)).toBe(want),
  );
  it.each<[number[], number]>([
    [[0, 2, 3, 5, 6, 7], 5],
    [[0, 2, 3, 5, 6, 8, 9, 10], 5],
    [[0, 2, 5, 6, 8, 9, 10], 5],
    [[0, 1, 2, 4, 5, 7], 5],
    [[0, 2, 3, 5, 6], 4],
    [[0, 1, 3, 4, 6, 7], 4],
  ])("TestLongestGoodRun.test_sliding_windows %j", (offsets, want) =>
    expect(longestGoodRun(offsets.map((o) => 100 + o))).toBe(want),
  );

  it.each<[string, number[], number, boolean]>([
    ["test_second_gap_slides_to_a_doable_slot", [0, 2, 3, 5, 6, 7], 5, true],
    ["test_run_meets_eighty_percent_floor", [0, 1, 2, 3, 4], 5.5, true],
    ["test_run_below_floor_is_not_doable", [0, 1, 2, 3], 5.5, false],
    ["test_exact_whole_hour_floor_with_float_epsilon", [0, 1, 2, 3], 5, true],
    ["test_one_bad_hour_in_slot_still_doable", [0, 1, 2, 4, 5], 5.5, true],
    ["test_long_walk_gets_dark_shoulder_credit", [0, 1, 2, 3, 4, 5], 10, true],
    [
      "test_long_walk_without_enough_run_not_doable",
      [0, 1, 2, 3, 4],
      10,
      false,
    ],
    ["test_short_walk_no_shoulder_credit", [0, 1, 2, 3, 4], 7, false],
    ["test_no_hours_not_doable", [], 3, false],
  ])("TestDoableDays.%s", (_name, offsets, duration, want) => {
    expect(doableDays(hours(offsets), duration)).toEqual(
      want ? ["2026-06-15"] : [],
    );
  });

  it.each([
    [2, 80, true],
    [2.1, 80, false],
    [2, 80.01, false],
    [0, 79.92, true],
    [0, 80.28, false],
  ])("weather thresholds: %s mm / %s km/h", (rain, wind, want) => {
    const row: Hour = [base, 12, Number(rain), Number(wind), 40, null, null];
    expect(viableDays([row], 1, base * 1000)).toEqual(
      want ? ["2026-06-15"] : [],
    );
  });

  it.each([
    [4, true],
    [5, true],
    [5.5, false],
  ])(
    "partial sunset hour already omitted by publisher: %s h",
    (duration, want) => {
      const times = [13, 14, 15, 16].map(
        (hour) => Date.parse(`2026-10-22T${hour}:00:00Z`) / 1000,
      );
      expect(doableDays(times, Number(duration))).toEqual(
        want ? ["2026-10-22"] : [],
      );
    },
  );
});

it("keeps the current clock hour, removes elapsed hours and reevaluates next hour", () => {
  expect(viableDays(rows([0, 1, 2, 3]), 5, (base + 1800) * 1000)).toEqual([
    "2026-06-15",
  ]);
  expect(viableDays(rows([0, 1, 2, 3]), 5, (base + 3600) * 1000)).toEqual([]);
  expect(viableDays(rows([0, 1, 2, 3]), 1, (base + 20 * 3600) * 1000)).toEqual(
    [],
  );
});
it("sorts and deduplicates hour buckets; requires real daylight and a positive duration", () => {
  expect(doableDays(hours([3, 0, 2, 1, 1, 1]), 5)).toEqual(["2026-06-15"]);
  expect(doableDays(hours([0, 0, 0, 0]), 5)).toEqual([]);
  expect(doableDays([], 10)).toEqual([]);
  expect(doableDays(hours([0]), 0)).toEqual([]);
});
it("uses UK days at summer midnight and keeps autumn repeated clock hours distinct", () => {
  expect(doableDays([Date.parse("2026-06-15T23:00:00Z") / 1000], 1)).toEqual([
    "2026-06-16",
  ]);
  const repeated = ["2026-10-25T00:00:00Z", "2026-10-25T01:00:00Z"].map(
    (s) => Date.parse(s) / 1000,
  );
  expect(doableDays(repeated, 2.5)).toEqual(["2026-10-25"]);
});
it.each(["2026-03-28T23:30:00Z", "2026-10-24T23:30:00Z"])(
  "day strip stays consecutive across DST: %s",
  (time) => {
    const days = upcomingDays(Date.parse(time));
    expect(new Set(days).size).toBe(7);
    for (let i = 1; i < days.length; i++)
      expect(Date.parse(days[i]!) - Date.parse(days[i - 1]!)).toBe(86_400_000);
  },
);
