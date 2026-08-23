import { afterEach, describe, expect, test, vi } from "vitest";
import { POST } from "./+server.js";

afterEach(() => vi.restoreAllMocks());

describe("run decision proxy", () => {
  test("forwards the decision body and passes through the response", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ decision: "approve" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    const request = new Request(
      "http://localhost/agents/runs/wf/nodes/gate/decision",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Cf-Access-Authenticated-User-Email": "human@example.com",
        },
        body: JSON.stringify({ decision: "approve", note: "ship it" }),
      },
    );

    const response = await POST({
      params: { id: "wf/1", key: "push gate" },
      request,
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ decision: "approve" });
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining(
        "/api/swarm/runs/wf%2F1/nodes/push%20gate/decision",
      ),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decision: "approve", note: "ship it" }),
        headers: expect.objectContaining({
          "Cf-Access-Authenticated-User-Email": "human@example.com",
        }),
      }),
    );
  });

  test("passes through an invalid-option response", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "choose approve or retry" }), {
          status: 422,
          headers: { "Content-Type": "application/json" },
        }),
    );
    const request = new Request("http://localhost", {
      method: "POST",
      body: JSON.stringify({ decision: "deny", note: "" }),
    });

    const response = await POST({
      params: { id: "wf-1", key: "push_gate" },
      request,
    });

    expect(response.status).toBe(422);
    expect(await response.json()).toEqual({
      detail: "choose approve or retry",
    });
  });

  test("returns the cancel proxy error shape when upstream is unavailable", async () => {
    global.fetch = vi.fn(() => {
      throw new Error("timeout");
    });
    const request = new Request("http://localhost", {
      method: "POST",
      body: JSON.stringify({ decision: "approve", note: "" }),
    });

    const response = await POST({
      params: { id: "wf-1", key: "push_gate" },
      request,
    });

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "swarm run unavailable" });
  });
});
