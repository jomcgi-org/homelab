import { beforeEach, describe, expect, test, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { POST } from "./+server.js";

function request(body, email = "owner@example.com") {
  return new Request("http://localhost/factory/execution/session/42/stop", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(email ? { "X-Auth-Email": email } : {}),
    },
    body: JSON.stringify(body),
  });
}

describe("session Stop proxy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  test("forwards the owner and exact observed turn identity", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ outcome: "requested" }), {
          status: 202,
        }),
    );

    const response = await POST({
      params: { id: "42" },
      request: request({ turn_seq: 7, dispatch_id: "dispatch-7", extra: true }),
    });

    expect(response.status).toBe(202);
    const [url, options] = global.fetch.mock.calls[0];
    expect(url).toBe("http://backend/api/agents/sessions/42/stop");
    expect(options.headers["X-Auth-Email"]).toBe("owner@example.com");
    expect(JSON.parse(options.body)).toEqual({
      turn_seq: 7,
      dispatch_id: "dispatch-7",
    });
  });

  test("reports relay loss as unknown", async () => {
    global.fetch = vi.fn(async () => {
      throw new Error("backend unavailable");
    });

    const response = await POST({
      params: { id: "42" },
      request: request({ turn_seq: 7, dispatch_id: "dispatch-7" }),
    });

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      outcome: "unknown",
      reason: "relay_unavailable",
    });
  });
});
