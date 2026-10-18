import { describe, expect, it } from "vitest";
import { clockTime, isInFlight, partitionRuns } from "./run-history.js";

describe("run history", () => {
  it("partitions by the DBOS status field", () => {
    const runs = [
      { workflow_id: "pending", dbos_status: "PENDING" },
      { workflow_id: "queued", dbos_status: "ENQUEUED" },
      { workflow_id: "done", dbos_status: "SUCCESS" },
      { workflow_id: "cancelled", dbos_status: "CANCELLED" },
    ];

    expect(partitionRuns(runs)).toEqual({
      inFlight: [runs[0], runs[1]],
      terminal: [runs[2], runs[3]],
    });
    expect(isInFlight(runs[0])).toBe(true);
    expect(isInFlight(runs[2])).toBe(false);
  });

  it("formats a timestamp as local 24 hour clock time", () => {
    // Local-time constructors make the expectation timezone independent
    // without recomputing it the way the implementation does.
    expect(clockTime(new Date(2026, 7, 22, 14, 2).toISOString())).toBe("14:02");
    expect(clockTime(new Date(2026, 7, 22, 9, 5).toISOString())).toBe("09:05");
    expect(clockTime("not-a-time")).toBe("");
    expect(clockTime(null)).toBe("");
  });
});
