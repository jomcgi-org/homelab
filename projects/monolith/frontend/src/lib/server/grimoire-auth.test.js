import { describe, expect, it, vi } from "vitest";
import { grimoireJson } from "./grimoire-auth.js";

const cookies = { get: () => "synthetic-token" };
const closedSocket = () =>
  new TypeError("fetch failed", { cause: { code: "UND_ERR_SOCKET" } });
const response = (value) => ({
  ok: true,
  status: 200,
  json: async () => value,
});

describe("Grimoire read transport", () => {
  it("retries a closed socket once with the same identity and deadline", async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(closedSocket())
      .mockResolvedValueOnce(response({ clue: "current detail" }));
    await expect(
      grimoireJson(fetch, cookies, "/campaigns/c/entities/e"),
    ).resolves.toEqual({ clue: "current detail" });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1]).toEqual(fetch.mock.calls[0]);
    expect(fetch.mock.calls[0][1].headers["x-grimoire-token"]).toBe(
      "synthetic-token",
    );
  });

  it("surfaces a second socket failure without looping", async () => {
    const failure = closedSocket();
    const fetch = vi.fn().mockRejectedValue(failure);
    await expect(grimoireJson(fetch, cookies, "/lobby")).rejects.toBe(failure);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each(["POST", "PATCH", "DELETE"])(
    "never replays a %s write",
    async (method) => {
      const failure = closedSocket();
      const fetch = vi.fn().mockRejectedValue(failure);
      await expect(
        grimoireJson(fetch, cookies, "/grants", { method }),
      ).rejects.toBe(failure);
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["ENOTFOUND", "UND_ERR_CONNECT_TIMEOUT", "ABORT_ERR"])(
    "does not mask %s failures",
    async (code) => {
      const failure = new TypeError("fetch failed", { cause: { code } });
      const fetch = vi.fn().mockRejectedValue(failure);
      await expect(grimoireJson(fetch, cookies, "/lobby")).rejects.toBe(
        failure,
      );
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );

  it("does not retry a socket failure after its deadline", async () => {
    const failure = closedSocket();
    const aborted = AbortSignal.abort();
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(aborted);
    try {
      const fetch = vi.fn().mockRejectedValue(failure);
      await expect(grimoireJson(fetch, cookies, "/lobby")).rejects.toBe(
        failure,
      );
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally {
      timeout.mockRestore();
    }
  });

  it.each([401, 403, 500])(
    "preserves HTTP %s without retrying",
    async (status) => {
      const fetch = vi.fn().mockResolvedValue({
        ok: false,
        status,
        json: async () => ({ detail: "refused" }),
      });
      await expect(
        grimoireJson(fetch, cookies, "/lobby"),
      ).rejects.toMatchObject({ status, message: "refused" });
      expect(fetch).toHaveBeenCalledTimes(1);
    },
  );
});
