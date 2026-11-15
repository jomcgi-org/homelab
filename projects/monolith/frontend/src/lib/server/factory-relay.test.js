import { afterEach, describe, expect, test, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { GET as drainLane } from "../../routes/private/factory/execution/drain-lane/+server.js";
import { GET as drainConsole } from "../../routes/private/factory/execution/drain/console/+server.js";

afterEach(() => vi.restoreAllMocks());

describe.each([
  ["drain lane", drainLane, "/api/agents/drain-lane"],
  ["drain console", drainConsole, "/api/agents/drain/console"],
])("%s proxy", (name, GET, path) => {
  test("relays the backend JSON", async () => {
    global.fetch = vi.fn(async () => Response.json({ rows: [1] }));

    const response = await GET();

    expect(global.fetch.mock.calls[0][0]).toBe(`http://backend${path}`);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(await response.json()).toEqual({ rows: [1] });
  });

  test.each([
    ["a non-ok status", async () => new Response("{}", { status: 500 })],
    [
      "a network error",
      async () => {
        throw new Error("timeout");
      },
    ],
  ])("answers 502 on %s", async (_, upstream) => {
    global.fetch = vi.fn(upstream);

    const response = await GET();

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: `${name} unavailable` });
  });
});
