import { describe, it, expect, vi } from "vitest";
import { load } from "./+page.server.js";

describe("/app/grimoire/chat load", () => {
  it("hydrates as not-admitted with no transcript when there is no session cookie", async () => {
    const fetch = vi.fn();
    const cookies = { get: () => undefined };
    const result = await load({ fetch, cookies });
    expect(result.admitted).toBe(false);
    expect(result.initialMessages).toEqual([]);
    expect(result.initialTokens).toBe(0);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rehydrates the stored transcript when a live session cookie is present", async () => {
    const messages = [
      { role: "user", content: "What are a beholder's lair actions?" },
      {
        role: "assistant",
        content: "According to the Monster Manual...",
        touched: [{ id: "beholder", title: "Beholder" }],
      },
    ];
    const fetch = vi.fn(async (url, init) => {
      expect(url).toMatch(/\/internal\/grimoire-chat\/transcript$/);
      expect(init.headers["X-Chat-Session-Id"]).toBe("sid-123");
      return { ok: true, json: async () => ({ messages, total_tokens: 42 }) };
    });
    const cookies = { get: () => "sid-123" };
    const result = await load({ fetch, cookies });
    expect(result.admitted).toBe(true);
    expect(result.initialMessages).toEqual(messages);
    expect(result.initialTokens).toBe(42);
  });

  it("falls back to the gate when the session cookie is stale (transcript 404)", async () => {
    const fetch = vi.fn(async () => ({ ok: false, status: 404 }));
    const cookies = { get: () => "stale-sid" };
    const result = await load({ fetch, cookies });
    expect(result.admitted).toBe(false);
    expect(result.initialMessages).toEqual([]);
  });

  it("fails soft to the gate on an upstream throw", async () => {
    const fetch = vi.fn(async () => {
      throw new Error("network blip");
    });
    const cookies = { get: () => "sid-123" };
    const result = await load({ fetch, cookies });
    expect(result.admitted).toBe(false);
    expect(result.initialMessages).toEqual([]);
    expect(result.initialTokens).toBe(0);
  });
});
