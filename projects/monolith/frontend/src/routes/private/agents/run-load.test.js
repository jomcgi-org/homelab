import { describe, expect, test } from "vitest";
import {
  RUNS_LOAD_MAX_ATTEMPTS,
  RUNS_LOAD_BACKOFF_MS,
  retryBackoffMs,
  degradeToTier,
} from "./run-load.js";

describe("run load retry strategy", () => {
  test("exports defined retry constants", () => {
    expect(RUNS_LOAD_MAX_ATTEMPTS).toBe(3);
    expect(RUNS_LOAD_BACKOFF_MS).toBe(200);
  });

  test("computes backoff as zero for the first attempt", () => {
    expect(retryBackoffMs(0)).toBe(0);
  });

  test("computes backoff multiplying attempt number by backoff ms", () => {
    expect(retryBackoffMs(1)).toBe(200);
    expect(retryBackoffMs(2)).toBe(400);
    expect(retryBackoffMs(3)).toBe(600);
  });

  test("degrades to stale tier when a snapshot already exists", () => {
    expect(degradeToTier(true)).toEqual({ engine_tier: "stale" });
  });

  test("degrades to absent tier on initial load with no prior snapshot", () => {
    expect(degradeToTier(false)).toEqual({ engine_tier: "absent" });
  });
});
