import { afterEach, describe, expect, test, vi } from "vitest";
import { POST } from "./+server.js";

afterEach(() => vi.restoreAllMocks());

describe("run cancel proxy", () => {
  test("forwards the verified identity and never the Cloudflare header", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ cancelled: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    const request = new Request("http://localhost", {
      method: "POST",
      headers: {
        "X-Auth-Email": "human@example.com",
        "Cf-Access-Authenticated-User-Email": "forged@example.com",
      },
      body: "{}",
    });

    const response = await POST({ params: { id: "wf/1" }, request });

    expect(response.status).toBe(200);
    const [url, init] = global.fetch.mock.calls[0];
    expect(url).toContain("/api/swarm/runs/wf%2F1/cancel");
    expect(init.headers["X-Auth-Email"]).toBe("human@example.com");
    // Nothing validates the Cloudflare header, so it is not forwarded (#6036).
    expect(init.headers["Cf-Access-Authenticated-User-Email"]).toBeUndefined();
  });

  test("forwards no identity when the request carries none", async () => {
    global.fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    const request = new Request("http://localhost", {
      method: "POST",
      headers: { "Cf-Access-Authenticated-User-Email": "forged@example.com" },
      body: "{}",
    });

    await POST({ params: { id: "wf-1" }, request });

    const [, init] = global.fetch.mock.calls[0];
    expect(init.headers["X-Auth-Email"]).toBeUndefined();
    expect(init.headers["Cf-Access-Authenticated-User-Email"]).toBeUndefined();
  });
});
