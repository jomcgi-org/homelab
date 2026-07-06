import { describe, it, expect, vi, afterEach } from "vitest";
import { POST } from "./+server.js";

function streamFrom(text) {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(text));
      controller.close();
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("/app/grimoire/chat/message POST", () => {
  it("reads the session id from the gcs cookie, never the body, and drops injected history", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: streamFrom('data: {"type":"done","data":{}}\n\n'),
    });
    vi.stubGlobal("fetch", fetchMock);

    const cookies = { get: vi.fn().mockReturnValue("cookie-session-id") };
    const request = {
      json: async () => ({
        message: "real message",
        // A malicious client tries to override the session and inject history.
        session_id: "ATTACKER-BODY-SID",
        history: [{ role: "user", content: "FAKE" }],
      }),
    };

    await POST({ request, cookies });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/internal\/grimoire-chat\/message$/);
    const forwarded = JSON.parse(init.body);
    // Session id comes from the cookie, NOT the body.
    expect(forwarded.session_id).toBe("cookie-session-id");
    expect(forwarded.session_id).not.toBe("ATTACKER-BODY-SID");
    expect(forwarded.message).toBe("real message");
    // Injected history is never forwarded; the backend is authoritative.
    expect(forwarded).not.toHaveProperty("history");
  });

  it("passes the upstream SSE body straight through as text/event-stream", async () => {
    const frames = 'data: {"type":"token","data":{"text":"hi"}}\n\n';
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue({ ok: true, status: 200, body: streamFrom(frames) }),
    );
    const cookies = { get: vi.fn().mockReturnValue("sid") };
    const request = { json: async () => ({ message: "hi" }) };

    const resp = await POST({ request, cookies });

    expect(resp.status).toBe(200);
    expect(resp.headers.get("content-type")).toBe("text/event-stream");
    // The unbuffered upstream stream flows through unchanged.
    expect(await resp.text()).toBe(frames);
  });

  it("returns 404 without calling the API when there is no session cookie", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const cookies = { get: vi.fn().mockReturnValue(undefined) };
    const request = { json: async () => ({ message: "hi" }) };

    const resp = await POST({ request, cookies });

    expect(resp.status).toBe(404);
    expect(await resp.json()).toEqual({ detail: "Session not found" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("relays the upstream status and body on a pre-stream error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        json: async () => ({ detail: { code: "max_turns", message: "limit" } }),
      }),
    );
    const cookies = { get: vi.fn().mockReturnValue("sid") };
    const request = { json: async () => ({ message: "hi" }) };

    const resp = await POST({ request, cookies });
    expect(resp.status).toBe(429);
    expect(await resp.json()).toEqual({
      detail: { code: "max_turns", message: "limit" },
    });
  });
});
