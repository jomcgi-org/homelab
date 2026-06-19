import { describe, it, expect, afterEach } from "vitest";
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
});
