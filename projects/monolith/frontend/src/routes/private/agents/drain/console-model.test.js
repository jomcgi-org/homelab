import { describe, expect, test } from "vitest";
import {
  RUNAWAY_CALLS,
  activityLine,
  ageSeconds,
  clockOffsetMs,
  filterJobs,
  fingerprintFraction,
  fmtClockAge,
  fmtDuration,
  isRunaway,
  jobCalls,
  jobClass,
  laneClass,
} from "./console-model.js";

describe("lane and job classes", () => {
  test("wedged and stranded are errors, running is ok", () => {
    expect(laneClass("wedged")).toBe("err");
    expect(laneClass("stranded")).toBe("err");
    expect(laneClass("running")).toBe("ok");
    expect(laneClass("quiet")).toBe("attn");
    expect(laneClass("nonsense")).toBe("idle");
  });

  test("failed jobs are errors, queued jobs ask for attention", () => {
    expect(jobClass("error")).toBe("err");
    expect(jobClass("due")).toBe("attn");
    expect(jobClass("ok")).toBe("idle");
  });
});

describe("server-clock ages", () => {
  test("offset corrects a skewed client clock", () => {
    const serverNow = "2026-08-28T12:00:00+00:00";
    const clientNow = Date.parse("2026-08-28T12:05:00+00:00");
    const offset = clockOffsetMs(serverNow, clientNow);
    expect(offset).toBe(-5 * 60 * 1000);
    // A checkpoint 30s before server-now reads 30s, not 5m30s.
    const age = ageSeconds("2026-08-28T11:59:30+00:00", offset, clientNow);
    expect(age).toBe(30);
  });

  test("garbage timestamps degrade to null, never NaN", () => {
    expect(ageSeconds(null)).toBeNull();
    expect(ageSeconds("not a date")).toBeNull();
    expect(clockOffsetMs(undefined)).toBe(0);
  });
});

describe("formatting", () => {
  test("clock age is precise below an hour", () => {
    expect(fmtClockAge(6)).toBe("6s");
    expect(fmtClockAge(248)).toBe("4m 08s");
    expect(fmtClockAge(4920)).toBe("1h 22m");
    expect(fmtClockAge(null)).toBe("");
  });

  test("duration rounds coarsely", () => {
    expect(fmtDuration(45)).toBe("45s");
    expect(fmtDuration(600)).toBe("10m");
    expect(fmtDuration(3900)).toBe("1h 5m");
  });
});

describe("job list", () => {
  test("filter keeps only the chosen state", () => {
    const jobs = [{ state: "error" }, { state: "ok" }];
    expect(filterJobs(jobs, "error")).toEqual([{ state: "error" }]);
    expect(filterJobs(jobs, "all")).toHaveLength(2);
    expect(filterJobs(null, "all")).toEqual([]);
  });

  test("live calls win for a running job", () => {
    const running = {
      state: "running",
      session: { live_calls: 17, calls: null },
    };
    const done = { state: "ok", session: { calls: 8 } };
    expect(jobCalls(running)).toBe(17);
    expect(jobCalls(done)).toBe(8);
    expect(jobCalls({ state: "due" })).toBeNull();
  });
});

describe("runaway fingerprint", () => {
  test("healthy and runaway counts are visually distinct", () => {
    const healthy = fingerprintFraction(8);
    const runaway = fingerprintFraction(434);
    expect(healthy).toBeGreaterThan(0);
    expect(runaway).toBeGreaterThan(healthy * 2);
    expect(runaway).toBeLessThanOrEqual(1);
  });

  test("the runaway line sits above measured-healthy and below measured-degenerate", () => {
    // 8 and 12 calls measured healthy; 434 and 461 measured degenerate.
    expect(isRunaway(12)).toBe(false);
    expect(isRunaway(434)).toBe(true);
    expect(RUNAWAY_CALLS).toBe(100);
  });
});

describe("activity lines", () => {
  test("renders the shapes Turns.svelte knows", () => {
    expect(activityLine("bash ls")).toBe("bash ls");
    expect(activityLine({ type: "bash", command: "git log" })).toBe(
      "bash git log",
    );
    expect(activityLine({ name: "read", input: { path: "a.py" } })).toBe(
      'read {"path":"a.py"}',
    );
    expect(activityLine(null)).toBe("");
  });
});
