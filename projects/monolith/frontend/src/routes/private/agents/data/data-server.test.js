import { beforeEach, describe, expect, it, vi } from "vitest";

// The route reads API_BASE at module scope, so stub it before the import
// (imports are hoisted; only vi.hoisted runs earlier).
vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { GET } from "./+server.js";

function jsonResponse(body, ok = true) {
  return {
    ok,
    status: ok ? 200 : 503,
    json: async () => body,
  };
}

describe("agents data poll endpoint", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    globalThis.fetch = undefined;
  });

  it("returns sessions plus the model catalogue from the backend", async () => {
    const sessions = [{ id: 7 }];
    globalThis.fetch = vi.fn(async (url) =>
      jsonResponse(
        String(url).includes("models")
          ? { models: [{ name: "qwen", family: "pi" }] }
          : sessions,
      ),
    );

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.sessions).toEqual(sessions);
    expect(body.models).toEqual([{ name: "qwen", family: "pi" }]);
  });

  it("serves an empty catalogue as empty rather than a bundled list", async () => {
    globalThis.fetch = vi.fn(async (url) =>
      jsonResponse(String(url).includes("models") ? { models: [] } : []),
    );

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(200);
    expect(body.models).toEqual([]);
  });

  it("answers 502 when either backend call fails", async () => {
    globalThis.fetch = vi.fn(async (url) =>
      jsonResponse([], !String(url).includes("models")),
    );

    const res = await GET();
    const body = await res.json();

    expect(res.status).toBe(502);
    expect(body.error).toBe("agents data unavailable");
  });
});
