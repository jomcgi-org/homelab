import { describe, it, expect, afterEach } from "vitest";
import { load } from "./+page.server.js";

describe("/public/app/notes load", () => {
  const original = process.env.TURNSTILE_SITE_KEY;
  afterEach(() => {
    if (original === undefined) delete process.env.TURNSTILE_SITE_KEY;
    else process.env.TURNSTILE_SITE_KEY = original;
  });

  it("exposes the public Turnstile site key and does not fetch the graph", () => {
    // The graph is fetched lazily client-side (via /app/notes/graph) only when
    // the visitor opens the graph view, so the page load stays light: it just
    // hands the public site key to the chat gate. No `fetch` is provided here on
    // purpose; the load must not call it.
    const result = load();
    expect(result).toHaveProperty("turnstileSiteKey");
  });
});
