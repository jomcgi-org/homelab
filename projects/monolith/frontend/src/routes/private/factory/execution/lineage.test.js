import { describe, expect, test } from "vitest";
import { crumbTrail, sessionLineage } from "./lineage.js";

describe("sessionLineage", () => {
  const run = {
    nodes: [
      {
        key: "implement",
        label: "implement",
        attempts: [{ session_id: 42, n: 2 }],
      },
    ],
  };

  test("matches numeric payload ids to string URL ids", () => {
    expect(sessionLineage(run, "42")).toEqual({
      nodeKey: "implement",
      nodeLabel: "implement",
      attemptN: 2,
    });
  });
  test("returns null for empty nodes", () => {
    expect(sessionLineage({ nodes: [] }, "42")).toBe(null);
  });
  test("returns null for a nullish run", () => {
    expect(sessionLineage(null, "42")).toBe(null);
  });
  test("returns null when no attempt matches", () => {
    expect(sessionLineage(run, "43")).toBe(null);
  });
  // ?run= with no ?session= derives lineage too. Stringified comparison makes
  // "null" equal "null", so without the guards an attempt that never got a
  // session would answer for the session nobody asked about.
  test("does not match an attempt with no session against no session", () => {
    const unstarted = {
      nodes: [{ key: "review", label: "review", attempts: [{ n: 1 }] }],
    };
    expect(sessionLineage(unstarted, null)).toBe(null);
    expect(sessionLineage(unstarted, undefined)).toBe(null);
  });
});

describe("crumbTrail", () => {
  test("renders a run trail", () => {
    expect(crumbTrail({ kind: "run", runTitle: "Fixture" })).toEqual([
      { label: "runs", to: "home" },
      { label: "Fixture", to: null },
    ]);
  });
  test("shortens a blank run trail", () => {
    expect(crumbTrail({ kind: "run", runTitle: " " })).toEqual([
      { label: "runs", to: "home" },
    ]);
  });
  test("renders session lineage", () => {
    expect(
      crumbTrail({
        kind: "session",
        runTitle: "Fixture",
        nodeLabel: "implement",
        attemptN: 2,
      }),
    ).toEqual([
      { label: "runs", to: "home" },
      { label: "Fixture", to: "run" },
      { label: "implement · attempt 2", to: null },
    ]);
  });
  test("falls back to the session title without lineage", () => {
    expect(
      crumbTrail({
        kind: "session",
        runTitle: "Fixture",
        sessionTitle: "Chat",
      }),
    ).toEqual([
      { label: "runs", to: "home" },
      { label: "Fixture", to: "run" },
      { label: "Chat", to: null },
    ]);
    expect(crumbTrail({ kind: "session", runTitle: "Fixture" })).toEqual([
      { label: "runs", to: "home" },
      { label: "Fixture", to: "run" },
    ]);
  });
  test("has no trail for standalone sessions or home", () => {
    expect(crumbTrail({ kind: "session" })).toEqual([]);
    expect(crumbTrail({ kind: "home" })).toEqual([]);
  });
});
