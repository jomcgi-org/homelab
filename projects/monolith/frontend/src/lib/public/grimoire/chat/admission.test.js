import { describe, it, expect, vi } from "vitest";
import { createChatSession, forkChatSession } from "./admission.js";

describe("createChatSession", () => {
  it("POSTs the turnstile token to the same-origin session proxy", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, status: 200 });

    const result = await createChatSession("tok-abc", fetchImpl);

    expect(result).toEqual({ ok: true, status: 200 });
    const [url, init] = fetchImpl.mock.calls[0];
    // Same-origin SSR proxy, not the internal chat API directly.
    expect(url).toBe("/app/grimoire/chat/session");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ turnstile_token: "tok-abc" });
  });

  it("relays a failed admission as ok:false with the upstream status", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 403 });

    const result = await createChatSession("bad-token", fetchImpl);

    expect(result).toEqual({ ok: false, status: 403 });
  });
});

describe("forkChatSession", () => {
  it("POSTs the snapshot id and turnstile token to the same-origin fork proxy", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, status: 200 });

    const result = await forkChatSession("snap-123", "tok-abc", fetchImpl);

    expect(result).toEqual({ ok: true, status: 200 });
    const [url, init] = fetchImpl.mock.calls[0];
    // Same-origin SSR proxy, not the internal chat API directly.
    expect(url).toBe("/app/grimoire/chat/fork");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      snapshot_id: "snap-123",
      turnstile_token: "tok-abc",
    });
  });

  it("relays a rejected fork as ok:false with the upstream status", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 404 });

    const result = await forkChatSession("missing", "tok", fetchImpl);

    expect(result).toEqual({ ok: false, status: 404 });
  });
});
