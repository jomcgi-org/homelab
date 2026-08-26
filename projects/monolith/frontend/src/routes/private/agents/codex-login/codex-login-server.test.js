import { beforeEach, describe, expect, test, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { GET } from "./status/+server.js";
import { POST } from "./start/+server.js";

describe("Codex login proxies", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = undefined;
  });

  test("GET forwards the optional grant with a 10 second timeout", async () => {
    const signal = new AbortController().signal;
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(signal);
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ state: "pending" }), { status: 200 }),
    );

    const response = await GET({
      url: new URL(
        "http://frontend/private/agents/codex-login/status?grant=codex-dev",
      ),
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ state: "pending" });
    expect(timeout).toHaveBeenCalledWith(10000);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      new URL("http://backend/api/agents/codex-login/status?grant=codex-dev"),
      { signal },
    );
  });

  test("POST forwards the optional grant with a timeout", async () => {
    const signal = new AbortController().signal;
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(signal);
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ user_code: "CODE" }), { status: 200 }),
    );

    const response = await POST({
      url: new URL(
        "http://frontend/private/agents/codex-login/start?grant=codex-dev",
      ),
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ user_code: "CODE" });
    expect(timeout).toHaveBeenCalledWith(10000);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      new URL("http://backend/api/agents/codex-login/start?grant=codex-dev"),
      { method: "POST", signal },
    );
  });

  test.each([
    ["status", GET],
    ["start", POST],
  ])("%s returns a JSON 502 when the backend throws", async (name, handler) => {
    globalThis.fetch = vi.fn(async () => Promise.reject(new Error("offline")));

    const response = await handler({
      url: new URL(`http://frontend/private/agents/codex-login/${name}`),
    });

    expect(response.status).toBe(502);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect((await response.json()).error).toContain("Codex login");
  });
});
