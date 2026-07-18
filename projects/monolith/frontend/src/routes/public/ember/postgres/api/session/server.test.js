import { describe, it, expect, vi } from "vitest";
import { POST } from "./+server.js";

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

function makeRequest({ cookie } = {}) {
  return {
    headers: makeHeaders(cookie ? { cookie } : {}),
    text: async () => JSON.stringify({ turnstile_token: "tok" }),
  };
}

function upstream({ setCookie } = {}) {
  return vi.fn().mockResolvedValue({
    status: 200,
    headers: makeHeaders(setCookie ? { "set-cookie": setCookie } : {}),
    text: async () => JSON.stringify({ ok: true }),
  });
}

describe("/ember/postgres/api/session POST", () => {
  it("rescopes the upstream cookie Path to the public proxy prefix", async () => {
    // The backend scopes demo_pg_session to its own mount
    // (Path=/api/ember/postgres), but the browser only ever requests
    // /ember/postgres/api/*: relayed verbatim, the cookie is never sent back
    // and every insert fails session_required despite a solved check.
    const fetch = upstream({
      setCookie:
        "demo_pg_session=abc123; HttpOnly; Max-Age=3600; " +
        "Path=/api/ember/postgres; SameSite=lax; Secure",
    });

    const resp = await POST({ request: makeRequest(), fetch });

    const relayed = resp.headers.get("set-cookie");
    expect(relayed).toContain("Path=/ember/postgres");
    expect(relayed).not.toContain("Path=/api/ember/postgres");
    // Everything else passes through untouched.
    expect(relayed).toContain("demo_pg_session=abc123");
    expect(relayed).toContain("HttpOnly");
    expect(relayed).toContain("Secure");
  });

  it("omits set-cookie when upstream minted nothing (existing session)", async () => {
    const fetch = upstream();
    const resp = await POST({
      request: makeRequest({ cookie: "demo_pg_session=abc" }),
      fetch,
    });
    expect(resp.headers.get("set-cookie")).toBeNull();
    expect(await resp.json()).toEqual({ ok: true });
  });

  it("forwards the inbound cookie header upstream so mint can no-op", async () => {
    const fetch = upstream();
    await POST({
      request: makeRequest({ cookie: "demo_pg_session=abc" }),
      fetch,
    });
    const [, init] = fetch.mock.calls[0];
    expect(init.headers.cookie).toBe("demo_pg_session=abc");
  });
});
