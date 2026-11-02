import { afterEach, describe, expect, it, vi } from "vitest";
import { grimoireJson } from "./grimoire-auth.js";

const cookies = { get: () => "local-test-token" };
const ok = () => new Response('{"ok":true}');
afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("staged Grimoire upstream recovery", () => {
  const enable = () => vi.stubEnv("GRIMOIRE_ROLLOUT_RECOVERY_ENABLED", "true");

  it.each([undefined, "GET", "get"])(
    "retries a connection failure for %s GET once",
    async (method) => {
      enable();
      const fetch = vi
        .fn()
        .mockRejectedValueOnce(new TypeError("fetch failed"))
        .mockResolvedValueOnce(ok());
      await expect(
        grimoireJson(fetch, cookies, "/lobby", { method }),
      ).resolves.toEqual({ ok: true });
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(fetch.mock.calls[1]).toEqual(fetch.mock.calls[0]);
    },
  );

  it.each([502, 503, 504])("retries HTTP %i once", async (status) => {
    enable();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response("unavailable", { status }))
      .mockResolvedValueOnce(ok());
    await expect(grimoireJson(fetch, cookies, "/lobby")).resolves.toEqual({
      ok: true,
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each([502, 503, 504])(
    "preserves final HTTP %i after the retry ceiling",
    async (status) => {
      enable();
      const fetch = vi.fn(
        async () =>
          new Response('{"detail":"upstream unavailable"}', { status }),
      );
      await expect(
        grimoireJson(fetch, cookies, "/lobby"),
      ).rejects.toMatchObject({ status, message: "upstream unavailable" });
      expect(fetch).toHaveBeenCalledTimes(2);
    },
  );

  it("bounds connection failures to two attempts", async () => {
    enable();
    const failure = new TypeError("fetch failed");
    const fetch = vi.fn().mockRejectedValue(failure);
    await expect(grimoireJson(fetch, cookies, "/lobby")).rejects.toBe(failure);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each(["POST", "PUT", "PATCH", "DELETE", "HEAD"])(
    "never retries %s",
    async (method) => {
      enable();
      for (const failure of [new TypeError("fetch failed"), 502, 503, 504]) {
        const fetch = vi.fn(async () => {
          if (typeof failure !== "number") throw failure;
          return new Response("unavailable", { status: failure });
        });
        await expect(
          grimoireJson(fetch, cookies, "/lobby", { method }),
        ).rejects.toThrow();
        expect(fetch).toHaveBeenCalledTimes(1);
      }
    },
  );

  it.each([400, 401, 403, 404, 409, 422, 429, 500])(
    "never retries HTTP %i",
    async (status) => {
      enable();
      const fetch = vi.fn(
        async () => new Response('{"detail":"rejected"}', { status }),
      );
      await expect(
        grimoireJson(fetch, cookies, "/lobby"),
      ).rejects.toMatchObject({ status });
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it("does not retry invalid JSON or an unrelated exception", async () => {
    enable();
    for (const fetch of [
      vi.fn(async () => new Response("invalid")),
      vi.fn(async () => {
        throw new Error("application failure");
      }),
    ]) {
      await expect(grimoireJson(fetch, cookies, "/lobby")).rejects.toThrow();
      expect(fetch).toHaveBeenCalledTimes(1);
    }
  });

  it("keeps identity, Headers, options and caller cancellation on both attempts", async () => {
    enable();
    const controller = new AbortController();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(ok());
    await grimoireJson(fetch, cookies, "/lobby", {
      headers: new Headers({
        accept: "application/json",
        "x-grimoire-token": "untrusted",
      }),
      signal: controller.signal,
      cache: "no-store",
    });
    const options = fetch.mock.calls[1][1];
    expect(options.headers.accept).toBe("application/json");
    expect(options.headers["x-grimoire-token"]).toBe("local-test-token");
    expect(options.cache).toBe("no-store");
    expect(options.signal).toBe(fetch.mock.calls[0][1].signal);
    controller.abort();
    expect(options.signal.aborted).toBe(true);
  });

  it("does not retry cancellation or a spent deadline", async () => {
    enable();
    for (const name of ["AbortError", "TimeoutError"]) {
      const fetch = vi
        .fn()
        .mockRejectedValue(new DOMException("stopped", name));
      await expect(
        grimoireJson(fetch, cookies, "/lobby"),
      ).rejects.toMatchObject({ name });
      expect(fetch).toHaveBeenCalledTimes(1);
    }
    const controller = new AbortController();
    const fetch = vi.fn(async () => {
      controller.abort();
      throw new TypeError("aborted connection");
    });
    await expect(
      grimoireJson(fetch, cookies, "/lobby", { signal: controller.signal }),
    ).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("keeps successful and empty responses single-attempt", async () => {
    enable();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(ok())
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(grimoireJson(fetch, cookies, "/lobby")).resolves.toEqual({
      ok: true,
    });
    await expect(grimoireJson(fetch, cookies, "/lobby")).resolves.toBeNull();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("shares one 1500ms GET budget and retains the mutation timeout", async () => {
    enable();
    const timeout = vi.spyOn(AbortSignal, "timeout");
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(ok())
      .mockResolvedValueOnce(ok());
    await grimoireJson(fetch, cookies, "/lobby");
    expect(timeout.mock.calls).toEqual([[1500]]);
    await grimoireJson(fetch, cookies, "/lobby", { method: "POST" });
    expect(timeout.mock.calls).toEqual([[1500], [10000]]);
  });

  it("does not fetch for an already cancelled request or missing identity", async () => {
    enable();
    const fetch = vi.fn();
    const signal = AbortSignal.abort();
    await expect(
      grimoireJson(fetch, cookies, "/lobby", { signal }),
    ).rejects.toMatchObject({ name: "AbortError" });
    await expect(
      grimoireJson(fetch, { get: () => undefined }, "/lobby"),
    ).rejects.toMatchObject({ status: 401 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("remains default-off until the staged delivery is enabled", async () => {
    vi.stubEnv("GRIMOIRE_ROLLOUT_RECOVERY_ENABLED", "false");
    const fetch = vi.fn().mockResolvedValue(new Response("", { status: 503 }));
    await expect(grimoireJson(fetch, cookies, "/lobby")).rejects.toMatchObject({
      status: 503,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
