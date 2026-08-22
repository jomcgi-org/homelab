import { afterEach, describe, expect, test, vi } from "vitest";
import { jumpActions, jumpMatches } from "./jump.js";

const NOW = new Date("2026-08-22T12:00:00Z");

function session(id, title, at, extra = {}) {
  return {
    id,
    title,
    status: "completed",
    last_turn_at: at,
    ...extra,
  };
}

function run(id, title, at, extra = {}) {
  return {
    workflow_id: id,
    title,
    state: "failed",
    completed_at: at,
    ...extra,
  };
}

function context(overrides = {}) {
  return {
    sessions: [],
    runs: [],
    terminalRuns: [],
    inbox: { needsYou: [], running: [] },
    ...overrides,
  };
}

afterEach(() => vi.useRealTimers());

describe("jump matches", () => {
  test("segments a title hit in the middle", () => {
    const result = jumpMatches(
      "palette",
      context({
        sessions: [
          session("s1", "Build the palette today", "2026-08-22T11:00:00Z"),
        ],
      }),
    );

    expect(result.earlier[0].segments).toEqual([
      { text: "Build the ", hit: false },
      { text: "palette", hit: true },
      { text: " today", hit: false },
    ]);
  });

  test("matches title casing without changing the rendered segment", () => {
    const result = jumpMatches(
      "BUILD",
      context({
        sessions: [session("s1", "Build the palette", "2026-08-22T11:00:00Z")],
      }),
    );

    expect(result.earlier[0].segments[0]).toEqual({
      text: "Build",
      hit: true,
    });
  });

  test("leaves an empty-query title as one non-hit segment", () => {
    const result = jumpMatches(
      "",
      context({
        sessions: [session("s1", "No highlight", "2026-08-22T11:00:00Z")],
      }),
    );

    expect(result.earlier[0].segments).toEqual([
      { text: "No highlight", hit: false },
    ]);
  });

  test("assigns inbox and earlier without duplicates", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const active = session(
      "active",
      "Waiting for input",
      "2026-08-22T11:48:00Z",
      { status: "needs_input" },
    );
    const groups = {
      needsYou: [
        {
          kind: "session",
          id: active.id,
          value: active,
          activityAt: active.last_turn_at,
        },
      ],
      running: [],
    };

    const result = jumpMatches(
      "",
      context({
        sessions: [
          active,
          session("done", "Earlier work", "2026-08-21T12:00:00Z"),
        ],
        inbox: groups,
      }),
    );

    expect(result.inbox.map((item) => item.id)).toEqual(["active"]);
    expect(result.inbox[0].meta).toBe("needs you · 12m");
    expect(result.earlier.map((item) => item.id)).toEqual(["done"]);
  });

  test("sorts earlier sessions and terminal runs by last activity", () => {
    const result = jumpMatches(
      "",
      context({
        sessions: [
          session("old", "Old session", "2026-08-20T09:00:00Z"),
          session("new", "New session", "2026-08-22T09:00:00Z"),
        ],
        terminalRuns: [run("middle", "Middle run", "2026-08-21T09:00:00Z")],
      }),
    );

    expect(result.earlier.map((item) => item.id)).toEqual([
      "new",
      "middle",
      "old",
    ]);
  });

  test("keeps all inbox rows and only eight earlier rows for an empty query", () => {
    const needs = run("needs", "Needs approval", "2026-08-22T11:00:00Z");
    const running = run("running", "Still running", "2026-08-22T10:00:00Z", {
      state: "running",
    });
    const result = jumpMatches(
      "",
      context({
        sessions: Array.from({ length: 10 }, (_, index) =>
          session(
            `s${index}`,
            `Session ${index}`,
            `2026-08-${String(10 + index).padStart(2, "0")}T09:00:00Z`,
          ),
        ),
        runs: [needs, running],
        inbox: {
          needsYou: [
            {
              kind: "run",
              id: needs.workflow_id,
              value: needs,
              activityAt: needs.completed_at,
            },
          ],
          running: [
            {
              kind: "run",
              id: running.workflow_id,
              value: running,
              activityAt: running.completed_at,
            },
          ],
        },
      }),
    );

    expect(result.inbox.map((item) => item.id)).toEqual(["needs", "running"]);
    expect(result.earlier).toHaveLength(8);
    expect(result.earlier[0].id).toBe("s9");
    expect(
      [...result.inbox, ...result.earlier].slice(0, 3).map(({ id }) => id),
    ).toEqual(["needs", "running", "s9"]);
  });
});

describe("jump actions", () => {
  test("offers a plain new session then voice for an empty query", () => {
    expect(jumpActions("")).toEqual([
      {
        kind: "new",
        id: "action-new",
        title: "New session",
        hint: "",
      },
      {
        kind: "voice",
        id: "action-voice",
        title: "Open voice companion",
        hint: "",
      },
    ]);
  });

  test("offers prefilled session and turn-search actions for a query", () => {
    expect(jumpActions("fix login")).toEqual([
      {
        kind: "new",
        id: "action-new",
        title: 'New session with "fix login"',
        hint: "↵",
      },
      {
        kind: "voice",
        id: "action-voice",
        title: "Open voice companion",
        hint: "",
      },
      {
        kind: "search",
        id: "action-search",
        title: 'Search turns for "fix login"',
        hint: "⇧↵",
      },
    ]);
  });
});
