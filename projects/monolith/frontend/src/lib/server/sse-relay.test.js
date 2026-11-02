import { afterEach, describe, expect, test, vi } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { POST as explore } from "../../routes/private/chat/+server.js";
import { POST as cluster } from "../../routes/private/dashboard-chat/+server.js";

afterEach(() => vi.restoreAllMocks());

const chatRequest = () =>
  new Request("http://localhost", {
    method: "POST",
    body: JSON.stringify({ message: "hi" }),
  });

describe.each([
  ["explore", explore, "/api/chat/explore"],
  ["cluster", cluster, "/api/chat/cluster"],
])("%s chat proxy", (_, POST, path) => {
  test("forwards the JSON body and streams the SSE reply", async () => {
    global.fetch = vi.fn(async () => new Response("data: hello\n\n"));

    const response = await POST({ request: chatRequest() });

    const [url, init] = global.fetch.mock.calls[0];
    expect(url).toBe(`http://backend${path}`);
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(init.body).toBe(JSON.stringify({ message: "hi" }));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    expect(response.headers.get("cache-control")).toBe("no-cache");
    expect(await response.text()).toBe("data: hello\n\n");
  });

  test("passes an upstream failure status through", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 429 }));

    const response = await POST({ request: chatRequest() });

    expect(response.status).toBe(429);
    expect(await response.json()).toEqual({ error: "upstream failed" });
  });
});
