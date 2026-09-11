import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const task = {
  issue_number: 5980,
  title: "Worker-delivered probes",
  nodes: [
    {
      node_key: "implement",
      model: "sol",
      attempts: [
        { attempt: 1, status: "failed", session_key: "factory:a:implement:1" },
        {
          attempt: 2,
          status: "succeeded",
          session_key: "factory:a:implement:2",
        },
      ],
    },
    {
      node_key: "verify:delivery",
      model: "opus",
      attempts: [
        {
          attempt: 1,
          status: "succeeded",
          session_key: "factory:a:verify:delivery:1",
        },
      ],
    },
    {
      node_key: "review",
      model: "opus",
      attempts: [{ attempt: 1, status: "admitted" }],
    },
  ],
};

const session = {
  snapshotted_at: "2026-09-11T14:32:00Z",
  session: { key: "factory:a:implement:2", turn_count: 1 },
  turns: [{ seq: 1, prompt: "go", result_text: "done" }],
};

function router({ taskStatus = 200, sessionStatus = 200 } = {}) {
  return vi.fn((path) => {
    if (path.startsWith("/slop/factory/data/tasks/")) {
      return Promise.resolve({
        ok: taskStatus === 200,
        status: taskStatus,
        headers: { get: () => null },
        json: async () => ({
          snapshotted_at: "x",
          policy: { max_attempts: 2 },
          task,
        }),
      });
    }
    return Promise.resolve({
      ok: sessionStatus === 200,
      status: sessionStatus,
      headers: { get: () => '"sess"' },
      json: async () => session,
    });
  });
}

describe("factory session loader", () => {
  it("reads the session key off the attempt rather than rebuilding it", async () => {
    const fetch = router();

    const result = await load({
      fetch,
      params: { issue: "5980", node: "implement", attempt: "2" },
      setHeaders: vi.fn(),
    });

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/slop/factory/data/tasks/5980",
      "/slop/factory/data/sessions/factory%3Aa%3Aimplement%3A2",
    ]);
    expect(result.attempt.attempt).toBe(2);
    expect(result.node.node_key).toBe("implement");
    expect(result.turns).toEqual(session.turns);
    expect(result.policy).toEqual({ max_attempts: 2 });
  });

  it("carries a node key that contains colons through to the session key", async () => {
    const fetch = router();

    await load({
      fetch,
      params: { issue: "5980", node: "verify:delivery", attempt: "1" },
      setHeaders: vi.fn(),
    });

    expect(fetch.mock.calls[1][0]).toBe(
      "/slop/factory/data/sessions/factory%3Aa%3Averify%3Adelivery%3A1",
    );
  });

  it("sets the one-minute cache policy from the session response", async () => {
    const setHeaders = vi.fn();

    await load({
      fetch: router(),
      params: { issue: "5980", node: "implement", attempt: "2" },
      setHeaders,
    });

    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "public, max-age=60, s-maxage=60",
      "cloudflare-cdn-cache-control": "public, max-age=60",
      etag: '"testbuild-sess"',
    });
  });

  it.each([
    ["nonexistent", "1"],
    ["implement", "9"],
    ["review", "1"],
  ])("404s on node %s attempt %s", async (node, attempt) => {
    const fetch = router();

    await expect(
      load({
        fetch,
        params: { issue: "5980", node, attempt },
        setHeaders: vi.fn(),
      }),
    ).rejects.toMatchObject({ status: 404 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("404s on an unknown task and 503s on an upstream outage", async () => {
    await expect(
      load({
        fetch: router({ taskStatus: 404 }),
        params: { issue: "1", node: "implement", attempt: "1" },
        setHeaders: vi.fn(),
      }),
    ).rejects.toMatchObject({ status: 404 });

    await expect(
      load({
        fetch: router({ sessionStatus: 500 }),
        params: { issue: "5980", node: "implement", attempt: "2" },
        setHeaders: vi.fn(),
      }),
    ).rejects.toMatchObject({ status: 503 });
  });
});
