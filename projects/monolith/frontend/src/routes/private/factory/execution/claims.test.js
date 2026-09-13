import { describe, expect, test } from "vitest";
import {
  claimStatus,
  freshestActivity,
  TERMINAL_QUIET_SECONDS,
} from "./claims.js";

const now = "2026-08-11T12:00:00Z";
const activity = (
  observed_at,
  nodeLabel = "implement",
  text = "git status",
) => ({
  label: nodeLabel,
  attempts: [{ live: { observed_at, activity: text } }],
});
const run = (state, options = {}) => ({ state, nodes: [], ...options });

describe("claimStatus", () => {
  test("does not unconfirm a terminal run without an observation", () => {
    expect(claimStatus(run("failed"), now)).toEqual({
      terminal: true,
      observation: null,
      unconfirmed: false,
    });
  });

  test("does not unconfirm activity before completion", () => {
    const result = claimStatus(
      run("failed", {
        completed_at: "2026-08-11T11:00:00Z",
        nodes: [activity("2026-08-11T10:59:00Z")],
      }),
      now,
    );
    expect(result.unconfirmed).toBe(false);
  });

  test("unconfirms activity after completion", () => {
    const result = claimStatus(
      run("failed", {
        completed_at: "2026-08-11T11:00:00Z",
        nodes: [activity("2026-08-11T11:00:01Z")],
      }),
      now,
    );
    expect(result.unconfirmed).toBe(true);
  });

  test("uses the quiet window for stranded runs", () => {
    expect(
      claimStatus(
        run("failed", {
          stranded: true,
          nodes: [activity("2026-08-11T11:59:55Z")],
        }),
        now,
      ).unconfirmed,
    ).toBe(true);
    expect(
      claimStatus(
        run("failed", {
          stranded: true,
          nodes: [
            activity(
              new Date(
                Date.parse(now) - (TERMINAL_QUIET_SECONDS + 1) * 1000,
              ).toISOString(),
            ),
          ],
        }),
        now,
      ).unconfirmed,
    ).toBe(false);
  });

  // The clause that motivates the feature. A run stranded by a deploy can
  // still report a live state, since `stranded` and `state` come from
  // different sources, and that contradiction is exactly what the console has
  // to render as a disagreement rather than as two confident facts.
  test("treats a stranded run as terminal even in a live state", () => {
    const result = claimStatus(
      run("running", {
        stranded: true,
        nodes: [activity("2026-08-11T11:59:55Z")],
      }),
      now,
    );
    expect(result.terminal).toBe(true);
    expect(result.unconfirmed).toBe(true);
  });

  test("never unconfirms a running run", () => {
    expect(
      claimStatus(
        run("running", { nodes: [activity("2026-08-11T11:59:55Z")] }),
        now,
      ).unconfirmed,
    ).toBe(false);
  });
});

test("freshestActivity picks the newest observation across nodes", () => {
  const result = freshestActivity({
    nodes: [
      activity("2026-08-11T11:58:00Z", "planner", "old command"),
      activity("2026-08-11T11:59:00Z", "implement", "new command"),
    ],
  });
  expect(result).toEqual({
    observedAt: "2026-08-11T11:59:00Z",
    nodeLabel: "implement",
    activity: "new command",
  });
});
