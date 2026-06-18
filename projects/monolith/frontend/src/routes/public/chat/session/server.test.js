import { describe, it, expect, vi, afterEach } from "vitest";
import { POST } from "./+server.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("/public/chat/session POST", () => {
  it("sets the opaque cps cookie httpOnly and never echoes the id in the body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ session_id: "opaque-session-id-123" }),
      }),
    );
    const cookies = { set: vi.fn() };
    const request = { json: async () => ({}), headers: makeHeaders() };

    const resp = await POST({ request, cookies });

    // The id lands in the httpOnly cookie, not the response body.
    expect(cookies.set).toHaveBeenCalledTimes(1);
    const [name, value, opts] = cookies.set.mock.calls[0];
    expect(name).toBe("cps");
    expect(value).toBe("opaque-session-id-123");
    expect(opts).toMatchObject({
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
    });

    const body = await resp.json();
    expect(body).toEqual({ ok: true });
    // The opaque id must never be readable by the browser via the body.
    expect(JSON.stringify(body)).not.toContain("opaque-session-id-123");
  });

  it("forwards the turnstile token and coarse geo to the internal API", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_id: "sid" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const cookies = { set: vi.fn() };
    const request = {
      json: async () => ({ turnstile_token: "tok-abc" }),
      headers: makeHeaders({ "cf-ipcountry": "GB", "user-agent": "UA/1" }),
    };

    await POST({ request, cookies });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/internal\/chat\/session$/);
    expect(JSON.parse(init.body)).toEqual({ turnstile_token: "tok-abc" });
    expect(init.headers["CF-IPCountry"]).toBe("GB");
    expect(init.headers["User-Agent"]).toBe("UA/1");
  });

  it("relays the upstream status when the internal API rejects", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue({ ok: false, status: 503, json: async () => ({}) }),
    );
    const cookies = { set: vi.fn() };
    const request = { json: async () => ({}), headers: makeHeaders() };

    const resp = await POST({ request, cookies });
    expect(resp.status).toBe(503);
    expect(cookies.set).not.toHaveBeenCalled();
  });
});
