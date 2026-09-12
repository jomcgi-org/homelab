import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { load } from "./+page.server.js";
import { GET } from "./+server.js";
import { POST } from "./decisions/[id]/+server.js";

function jsonResponse(body, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => body };
}

describe("agents /private/agents/escalations load", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = undefined;
  });

  it("takes the escalation list off the board", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ ok: true, escalations: [{ receipt_id: 3, open: true }] }),
    );

    const result = await load({ fetch: fetchMock });

    expect(result).toEqual({
      escalations: [{ receipt_id: 3, open: true }],
      error: false,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://backend/api/agents/factory",
      expect.anything(),
    );
  });

  it("reports an unavailable backend without throwing", async () => {
    const result = await load({
      fetch: vi.fn(async () => jsonResponse({}, false)),
    });
    expect(result).toEqual({ escalations: [], error: true });
  });

  it("a board with no escalations key still loads", async () => {
    const result = await load({
      fetch: vi.fn(async () => jsonResponse({ ok: true })),
    });
    expect(result).toEqual({ escalations: [], error: false });
  });

  it("relays the list through the proxy and 502s when the backend is down", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ escalations: [{ receipt_id: 3 }] }),
    }));
    const ok = await GET();
    expect(await ok.json()).toEqual({ escalations: [{ receipt_id: 3 }] });

    globalThis.fetch = vi.fn(async () => ({ ok: false, status: 503 }));
    const down = await GET();
    expect(down.status).toBe(502);
    expect(await down.json()).toEqual({ error: "escalations unavailable" });
  });
});

describe("the decision write proxy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = undefined;
  });

  function request(body, headers = {}) {
    return {
      headers: { get: (name) => headers[name] ?? null },
      text: async () => JSON.stringify(body),
    };
  }

  it("forwards the verified X-Auth-Email and the body, and relays the status", async () => {
    globalThis.fetch = vi.fn(async () => ({
      status: 200,
      text: async () => JSON.stringify({ ok: true }),
    }));

    const res = await POST({
      params: { id: "3" },
      request: request(
        { option_key: "close" },
        { "x-auth-email": "joe@example.test" },
      ),
    });

    expect(res.status).toBe(200);
    const [url, init] = globalThis.fetch.mock.calls[0];
    expect(url).toBe("http://backend/api/agents/factory/decisions/3");
    expect(init.headers["X-Auth-Email"]).toBe("joe@example.test");
    expect(JSON.parse(init.body)).toEqual({ option_key: "close" });
  });

  it("never forwards the unvalidated Cloudflare identity header", async () => {
    globalThis.fetch = vi.fn(async () => ({
      status: 403,
      text: async () => JSON.stringify({ detail: "not a factory operator" }),
    }));

    const res = await POST({
      params: { id: "3" },
      request: request(
        { option_key: "close" },
        { "cf-access-authenticated-user-email": "forged@example.test" },
      ),
    });

    expect(res.status).toBe(403);
    const [, init] = globalThis.fetch.mock.calls[0];
    expect(init.headers["X-Auth-Email"]).toBeUndefined();
    expect(init.headers["Cf-Access-Authenticated-User-Email"]).toBeUndefined();
  });

  it("answers 502 when the backend cannot be reached", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error("down");
    });

    const res = await POST({
      params: { id: "3" },
      request: request({ option_key: "close" }),
    });

    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ detail: "decision unavailable" });
  });
});
