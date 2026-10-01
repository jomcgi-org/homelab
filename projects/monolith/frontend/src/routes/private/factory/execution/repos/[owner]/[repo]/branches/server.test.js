import { describe, expect, test, vi } from "vitest";
import { GET as getBranches } from "./+server.js";

describe("branches proxy", () => {
  test("forwards branch list from API", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            branches: [{ name: "main" }, { name: "develop" }],
            default_branch: "main",
          }),
          { ok: true, status: 200 },
        ),
    );

    const response = await getBranches({
      params: { owner: "owner", repo: "repo" },
    });
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.branches).toEqual([{ name: "main" }, { name: "develop" }]);
    expect(body.default_branch).toBe("main");
  });

  test("encodes path parameters correctly", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ branches: [] }), {
          ok: true,
          status: 200,
        }),
    );

    await getBranches({ params: { owner: "org/name", repo: "my-repo" } });

    const callUrl = global.fetch.mock.calls[0][0];
    expect(callUrl).toContain("org%2Fname");
    expect(callUrl).toContain("my-repo");
  });

  test("forwards upstream status and error body on failure", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ error: "not found" }), {
          ok: false,
          status: 503,
        }),
    );

    const response = await getBranches({
      params: { owner: "owner", repo: "repo" },
    });
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(body.error).toBe("not found");
  });

  test("handles API timeout", async () => {
    global.fetch = vi.fn(() => {
      throw new Error("timeout");
    });

    const response = await getBranches({
      params: { owner: "owner", repo: "repo" },
    });
    const body = await response.json();

    expect(response.status).toBe(502);
    expect(body.error).toBe("branches unavailable");
  });

  test("handles null default_branch", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            branches: [{ name: "main" }],
            default_branch: null,
          }),
          { ok: true, status: 200 },
        ),
    );

    const response = await getBranches({
      params: { owner: "owner", repo: "repo" },
    });
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.default_branch).toBeNull();
  });
});
