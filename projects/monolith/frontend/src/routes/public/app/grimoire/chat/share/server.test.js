import { describe, it, expect, vi, afterEach } from "vitest";
import { POST } from "./+server.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("/app/grimoire/chat/share POST", () => {
  it("reads the session id from the gcs cookie, never the body, and forwards no transcript", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ snapshot_id: "snap-abc" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const cookies = { get: vi.fn().mockReturnValue("cookie-session-id") };

    const resp = await POST({ cookies });

    const [calledUrl, init] = fetchMock.mock.calls[0];
    expect(calledUrl).toMatch(/\/internal\/grimoire-chat\/share$/);
    const forwarded = JSON.parse(init.body);
    // Session id comes from the cookie; nothing else is forwarded (no client
    // transcript content can reach the snapshot).
    expect(forwarded).toEqual({ session_id: "cookie-session-id" });

    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ snapshot_id: "snap-abc" });
  });

  it("returns 404 without calling the API when there is no session cookie", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const cookies = { get: vi.fn().mockReturnValue(undefined) };

    const resp = await POST({ cookies });

    expect(resp.status).toBe(404);
    expect(await resp.json()).toEqual({ detail: "Session not found" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("relays the upstream status and body on an error (e.g. nothing to share)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        json: async () => ({ detail: "Nothing to share yet" }),
      }),
    );
    const cookies = { get: vi.fn().mockReturnValue("sid") };

    const resp = await POST({ cookies });
    expect(resp.status).toBe(400);
    expect(await resp.json()).toEqual({ detail: "Nothing to share yet" });
  });
});
