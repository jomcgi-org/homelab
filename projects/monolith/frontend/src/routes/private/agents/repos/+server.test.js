import { describe, expect, test, vi } from "vitest";
import { GET as getRepos } from "./+server.js";

describe("repos proxy", () => {
  test("forwards repo field from API", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ repos: [{ id: "owner/repo" }] }), {
          ok: true,
          status: 200,
        }),
    );

    const response = await getRepos();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.repos).toEqual([{ id: "owner/repo" }]);
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/agents/repos"),
      expect.any(Object),
    );
  });

  test("forwards upstream status and error body on failure", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "service unavailable" }), {
          ok: false,
          status: 503,
        }),
    );

    const response = await getRepos();
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(body.error).toBe("service unavailable");
  });

  test("handles API timeout", async () => {
    global.fetch = vi.fn(() => {
      throw new Error("timeout");
    });

    const response = await getRepos();
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(body.error).toBe("repos unavailable");
  });
});
