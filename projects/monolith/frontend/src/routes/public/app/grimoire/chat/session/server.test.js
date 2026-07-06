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

describe("/app/grimoire/chat/session POST", () => {
  it("sets the opaque gcs cookie httpOnly and never echoes the id in the body", async () => {
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
    expect(name).toBe("gcs");
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

  it("forwards the turnstile token, coarse geo, and client IP to the internal API", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_id: "sid" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const cookies = { set: vi.fn() };
    const request = {
      json: async () => ({ turnstile_token: "tok-abc" }),
      headers: makeHeaders({
        "cf-ipcountry": "GB",
        "user-agent": "UA/1",
        "cf-connecting-ip": "203.0.113.7",
      }),
    };

    await POST({ request, cookies });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/internal\/grimoire-chat\/session$/);
    expect(JSON.parse(init.body)).toEqual({ turnstile_token: "tok-abc" });
    expect(init.headers["CF-IPCountry"]).toBe("GB");
    expect(init.headers["User-Agent"]).toBe("UA/1");
    // The real client IP is forwarded so the backend can salt-and-hash it
    // (ip_hash); the backend trusts this header only from SSR's mesh identity.
    expect(init.headers["CF-Connecting-IP"]).toBe("203.0.113.7");
  });

  it("omits CF-Connecting-IP when the client IP header is absent", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_id: "sid" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const cookies = { set: vi.fn() };
    const request = { json: async () => ({}), headers: makeHeaders() };

    await POST({ request, cookies });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers["CF-Connecting-IP"]).toBeUndefined();
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
