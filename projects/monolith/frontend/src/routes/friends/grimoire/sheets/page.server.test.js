import { beforeEach, expect, it, vi } from "vitest";

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
  vi.resetModules();
});

it("loads sheets through the dedicated identity without forwarding operator headers", async () => {
  const { load } = await import("./+page.server.js");
  const fetch = vi
    .fn()
    .mockResolvedValue({ ok: true, status: 200, json: async () => [] });
  const result = await load({
    fetch,
    cookies: { get: () => "signed-grimoire-token" },
    setHeaders: vi.fn(),
    request: new Request("https://friends.jomcgi.dev/grimoire/sheets", {
      headers: {
        authorization: "Bearer operator",
        "x-auth-email": "forged@example.test",
      },
    }),
  });
  expect(result.unavailable).toBe(false);
  const headers = fetch.mock.calls[0][1].headers;
  expect(headers.get("x-grimoire-token")).toBe("signed-grimoire-token");
  expect(headers.has("authorization")).toBe(false);
  expect(headers.has("x-auth-email")).toBe(false);
});

it("keeps the submitted form body while substituting the verified cookie", async () => {
  const { actions } = await import("./+page.server.js");
  const fetch = vi
    .fn()
    .mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
  const body = new FormData();
  body.set("campaign_id", "11111111-1111-4111-8111-111111111111");
  body.set("character_id", "22222222-2222-4222-8222-222222222222");
  body.set("version_id", "33333333-3333-4333-8333-333333333333");
  const result = await actions.submit({
    fetch,
    cookies: { get: () => "signed" },
    request: new Request("https://friends.jomcgi.dev/grimoire/sheets", {
      method: "POST",
      body,
    }),
  });
  expect(result.ok).toBe(true);
  expect(fetch.mock.calls[0][0]).toContain(
    "/33333333-3333-4333-8333-333333333333/submit",
  );
});
