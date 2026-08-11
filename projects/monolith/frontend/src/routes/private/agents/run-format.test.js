import { describe, expect, test } from "vitest";
import {
  attemptMeta,
  engineStale,
  fmtCost,
  fmtDur,
  joinMeta,
  ordinal,
  relSeconds,
  spendOfBudget,
  startedAgo,
} from "./run-format.js";

describe("run formatting", () => {
  test("formats durations using the contract units", () => {
    expect(fmtDur(12)).toBe("12s");
    expect(fmtDur(90)).toBe("1m");
    expect(fmtDur(3660)).toBe("1h 1m");
    expect(fmtDur(172800)).toBe("2d");
  });
  test("formats costs and ordinals", () => {
    expect(fmtCost(0.004)).toBe("$0.0040");
    expect(fmtCost(2)).toBe("$2.00");
    expect(ordinal(3)).toBe("3rd");
    expect(ordinal(8)).toBe("8th");
  });
  test("calculates relative seconds", () =>
    expect(relSeconds("2026-08-10T00:00:00Z", "2026-08-10T00:01:02Z")).toBe(
      62,
    ));

  test("reports an unreadable timestamp as absent, not as zero", () => {
    // The observed defect: a run header rendered "started 0s ago" beside a
    // real "attempt 1 · 2m", because an unparseable timestamp became NaN and
    // `Number(x) || 0` laundered it into a measurement.
    expect(relSeconds("not-a-timestamp", "2026-08-10T00:00:00Z")).toBe(null);
    expect(relSeconds(undefined, "2026-08-10T00:00:00Z")).toBe(null);
    expect(fmtDur(relSeconds(null, "2026-08-10T00:00:00Z"))).toBe(null);
  });

  test("distinguishes a measured zero from an absent value", () => {
    expect(fmtDur(0)).toBe("0s");
    expect(fmtDur(null)).toBe(null);
    expect(fmtDur(undefined)).toBe(null);
    expect(fmtDur(NaN)).toBe(null);
    // Zero spend is measured, so it stays falsy-but-present; no spend at all
    // is null, so a sentence can omit the clause instead of printing "of
    // $50.00 budget" with nothing in front of it.
    expect(fmtCost(0)).toBe("");
    expect(fmtCost(null)).toBe(null);
    expect(fmtCost(undefined)).toBe(null);
  });
});

describe("phrases", () => {
  test("joins with its own separator and drops absent parts", () => {
    expect(joinMeta("a", "b")).toBe("a · b");
    expect(joinMeta("a", null, "", undefined, "b")).toBe("a · b");
    expect(joinMeta(null, undefined)).toBe(null);
  });

  test("spend is whole or absent, never a headless fragment", () => {
    // The observed defect: a run with no spend yet printed "of $50.00 budget".
    expect(spendOfBudget(0.2, 0.15)).toBe("$0.20 of $0.15 budget");
    expect(spendOfBudget(0, 0.15)).toBe("nothing spent yet");
    expect(spendOfBudget(null, 50)).toBe(null);
    expect(spendOfBudget(0.2, null)).toBe("$0.20");
    expect(spendOfBudget(null, null)).toBe(null);
  });

  test("attempt meta omits the parts it does not have", () => {
    expect(attemptMeta(2, "running", 1200, 0.12)).toBe(
      "attempt 2 · running 20m · $0.12",
    );
    expect(attemptMeta(1, null, 480, 0)).toBe("attempt 1 · 8m");
    expect(attemptMeta(1, "running", null, null)).toBe("attempt 1 · running");
  });

  test("a claim survives without the measurement it would have carried", () => {
    expect(startedAgo(120)).toBe("started 2m ago");
    expect(startedAgo(null)).toBe(null);
    // Dropping to the bare claim beats asserting the snapshot is "0s old",
    // which would read as fresh.
    expect(engineStale(120)).toBe("engine: unreachable, showing 2m old state");
    expect(engineStale(null)).toBe("engine: unreachable");
  });
});
