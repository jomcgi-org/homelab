import { beforeEach, describe, expect, test, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { POST } from "./+server.js";

describe("agent session prewarm proxy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = undefined;
  });

  test("forwards the session id with a 10 second timeout", async () => {
    const signal = new AbortController().signal;
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(signal);
    globalThis.fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const request = new Request("http://frontend/private/agents/prewarm", {
      method: "POST",
      body: JSON.stringify({ session_id: 42 }),
    });

    const response = await POST({ request });

    expect(response.status).toBe(204);
    expect(timeout).toHaveBeenCalledWith(10000);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      "http://backend/api/agents/sessions/42/prewarm",
      { method: "POST", signal },
    );
  });

  test("returns 204 when the backend throws", async () => {
    globalThis.fetch = vi.fn(async () => Promise.reject(new Error("offline")));
    const request = new Request("http://frontend/private/agents/prewarm", {
      method: "POST",
      body: JSON.stringify({ session_id: 42 }),
    });

    const response = await POST({ request });

    expect(response.status).toBe(204);
    expect(response.body).toBeNull();
  });

  test("returns 204 without forwarding when the session id is absent", async () => {
    globalThis.fetch = vi.fn();
    const request = new Request("http://frontend/private/agents/prewarm", {
      method: "POST",
      body: JSON.stringify({}),
    });

    const response = await POST({ request });

    expect(response.status).toBe(204);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});
