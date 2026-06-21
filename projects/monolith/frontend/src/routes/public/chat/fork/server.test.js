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

describe("/public/chat/fork POST", () => {
  it("forks a snapshot into a session, setting the opaque cps cookie httpOnly", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_id: "forked-session-id-123" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const cookies = { set: vi.fn() };
    const request = {
      json: async () => ({
        snapshot_id: "snap-abc",
        turnstile_token: "tok-xyz",
      }),
      headers: makeHeaders(),
    };

    const resp = await POST({ request, cookies });

    // The internal API is forked with the snapshot id + the solved token.
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/internal\/chat\/fork$/);
    expect(JSON.parse(init.body)).toEqual({
      snapshot_id: "snap-abc",
      turnstile_token: "tok-xyz",
    });

    // The new session id lands in the httpOnly cookie, not the response body.
    expect(cookies.set).toHaveBeenCalledTimes(1);
    const [name, value, opts] = cookies.set.mock.calls[0];
    expect(name).toBe("cps");
    expect(value).toBe("forked-session-id-123");
    expect(opts).toMatchObject({
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
    });

    const body = await resp.json();
    expect(body).toEqual({ ok: true });
    expect(JSON.stringify(body)).not.toContain("forked-session-id-123");
  });

  it("forwards coarse geo + client IP for the new session's pseudonymous row", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ session_id: "sid" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const cookies = { set: vi.fn() };
    const request = {
      json: async () => ({ snapshot_id: "snap", turnstile_token: "tok" }),
      headers: makeHeaders({
        "cf-ipcountry": "GB",
        "user-agent": "UA/1",
        "cf-connecting-ip": "203.0.113.7",
      }),
    };

    await POST({ request, cookies });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers["CF-IPCountry"]).toBe("GB");
    expect(init.headers["User-Agent"]).toBe("UA/1");
    expect(init.headers["CF-Connecting-IP"]).toBe("203.0.113.7");
  });

  it("relays the upstream status and sets no cookie when the fork is rejected", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }),
    );
    const cookies = { set: vi.fn() };
    const request = {
      json: async () => ({ snapshot_id: "missing", turnstile_token: "tok" }),
      headers: makeHeaders(),
    };

    const resp = await POST({ request, cookies });
    expect(resp.status).toBe(404);
    expect(cookies.set).not.toHaveBeenCalled();
  });
});
