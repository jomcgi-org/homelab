import { afterEach, describe, expect, test, vi } from "vitest";
import { POST } from "./+server.js";

afterEach(() => vi.restoreAllMocks());

describe("exact session turn stop proxy", () => {
  test("forwards the verified operator identity and exact stop body", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            status: "requested",
            outcome: "awaiting_terminal_confirmation",
          }),
          { status: 200 },
        ),
    );
    const body = {
      guest_id: "guest-1",
      seq: 3,
      dispatch_id: "a".repeat(64),
    };
    const request = new Request("http://localhost/stop", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Auth-Email": "operator@example.com",
      },
      body: JSON.stringify(body),
    });

    const response = await POST({ params: { id: "7" }, request });

    const [url, init] = global.fetch.mock.calls[0];
    expect(url).toMatch(/\/api\/agents\/sessions\/7\/stop$/);
    expect(init.headers["X-Auth-Email"]).toBe("operator@example.com");
    expect(JSON.parse(init.body)).toEqual(body);
    expect(response.status).toBe(200);
    expect((await response.json()).status).toBe("requested");
  });
});
