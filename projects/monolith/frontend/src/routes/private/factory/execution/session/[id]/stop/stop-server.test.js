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
    delete process.env.AGENT_SESSION_STOP_CONTROL_ENABLED;
  });

  test("is inert by default and does not contact the backend", async () => {
    global.fetch = vi.fn();

    const response = await POST({
      params: { id: "42" },
      request: request({ turn_seq: 7, dispatch_id: "dispatch-7" }),
    });

    expect(response.status).toBe(404);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test("forwards the owner and exact observed turn identity when enabled", async () => {
    process.env.AGENT_SESSION_STOP_CONTROL_ENABLED = "true";
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
    process.env.AGENT_SESSION_STOP_CONTROL_ENABLED = "true";
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
