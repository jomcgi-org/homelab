import { describe, it, expect, afterEach, vi } from "vitest";
import { load } from "./+page.server.js";

describe("/public/app/notes load", () => {
  const original = process.env.TURNSTILE_SITE_KEY;
  afterEach(() => {
    if (original === undefined) delete process.env.TURNSTILE_SITE_KEY;
    else process.env.TURNSTILE_SITE_KEY = original;
  });

  it("exposes the public Turnstile site key and seeds the stats snapshot", async () => {
    // The graph is still fetched lazily client-side; load only seeds the chat
    // gate's site key plus the ticker's GPU/system snapshot from the same-origin
    // /app/notes/stats proxy.
    const fetch = async (url) => {
      expect(url).toBe("/app/notes/stats");
      return { ok: true, json: async () => ({ gpu: { utilization_pct: 42 } }) };
    };
    const result = await load({ fetch });
    expect(result).toHaveProperty("turnstileSiteKey");
    expect(result.stats).toEqual({ gpu: { utilization_pct: 42 } });
  });

  it("degrades to null stats when the snapshot is unavailable", async () => {
    // A stats hiccup must never block the chat: a non-ok response (or a throw)
    // leaves stats null and the ticker falls back to its static readouts.
    const fetch = async () => ({ ok: false });
    const result = await load({ fetch });
    expect(result).toHaveProperty("turnstileSiteKey");
    expect(result.stats).toBeNull();
  });

  it("hydrates as not-admitted with no transcript when there is no session cookie", async () => {
    // A cookieless visitor lands at the admission gate: the transcript endpoint
    // is never called, so the only fetch is the stats snapshot.
    const fetch = vi.fn(async (url) => {
      expect(url).toBe("/app/notes/stats");
      return { ok: true, json: async () => ({}) };
    });
    const cookies = { get: () => undefined };
    const result = await load({ fetch, cookies });
    expect(result.admitted).toBe(false);
    expect(result.initialMessages).toEqual([]);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rehydrates the stored transcript when a live session cookie is present", async () => {
    // A reload or a freshly-forked session resumes: the loader reads the cps
    // cookie, fetches the stored transcript, and hydrates admitted + messages.
    const messages = [
      { role: "user", content: "What is STPA?", touched: [] },
      {
        role: "assistant",
        content: "STPA is a hazard analysis method.",
        touched: [{ id: "stpa", title: "STPA" }],
      },
    ];
    const fetch = vi.fn(async (url, init) => {
      if (url === "/app/notes/stats") {
        return { ok: true, json: async () => ({}) };
      }
      // The transcript fetch forwards the session id from the cookie as a header.
      expect(url).toMatch(/\/internal\/chat\/transcript$/);
      expect(init.headers["X-Chat-Session-Id"]).toBe("sid-123");
      return {
        ok: true,
        json: async () => ({ messages, total_tokens: 42 }),
      };
    });
    const cookies = { get: () => "sid-123" };
    const result = await load({ fetch, cookies });
    expect(result.admitted).toBe(true);
    expect(result.initialMessages).toEqual(messages);
    expect(result.initialTokens).toBe(42);
  });

  it("falls back to the gate when the session cookie is stale (transcript 404)", async () => {
    // An expired/invalid cookie 404s on the transcript: hydrate as not-admitted
    // so the visitor re-passes the gate rather than seeing an empty session.
    const fetch = vi.fn(async (url) => {
      if (url === "/app/notes/stats") {
        return { ok: true, json: async () => ({}) };
      }
      return { ok: false, status: 404 };
    });
    const cookies = { get: () => "stale-sid" };
    const result = await load({ fetch, cookies });
    expect(result.admitted).toBe(false);
    expect(result.initialMessages).toEqual([]);
  });
});
