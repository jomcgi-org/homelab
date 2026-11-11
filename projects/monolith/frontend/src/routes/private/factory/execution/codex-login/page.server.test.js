import { describe, expect, test, vi } from "vitest";
import { load } from "./+page.server.js";

function request(search = "", body = { state: "none" }) {
  return {
    url: new URL(
      `https://private.jomcgi.dev/factory/execution/codex-login${search}`,
    ),
    fetch: vi.fn(async () => Response.json(body)),
    setHeaders: vi.fn(),
  };
}

describe("Codex login page load", () => {
  test("defaults to a production status read without starting login", async () => {
    const event = request();

    expect(await load(event)).toEqual({ environment: "prod", state: "none" });
    expect(event.fetch).toHaveBeenCalledOnce();
    const [url, options] = event.fetch.mock.calls[0];
    expect(url).toBe(
      "/private/factory/execution/codex-login/status?grant=codex-cluster&environment=prod",
    );
    expect(options.method).toBeUndefined();
    expect(options.cache).toBe("no-store");
    expect(event.setHeaders).toHaveBeenCalledWith({
      "cache-control": "private, no-store",
    });
  });

  test("reads only dev status for the direct dev link and shapes the response", async () => {
    const event = request("?environment=dev", {
      state: "granted",
      environment: "dev",
      grant: "codex-cluster",
      detail: "provider detail",
      extra: "discard this field",
    });

    expect(await load(event)).toEqual({ environment: "dev", state: "granted" });
    expect(event.fetch).toHaveBeenCalledOnce();
    expect(event.fetch.mock.calls[0][0]).toBe(
      "/private/factory/execution/codex-login/status?grant=codex-cluster&environment=dev",
    );
    expect(event.fetch.mock.calls[0][1].method).toBeUndefined();
  });

  test.each([
    "?environment=",
    "?environment=staging",
    "?environment=DEV",
    "?environment=prod&environment=dev",
  ])("rejects %s before any broker read", async (search) => {
    const event = request(search);

    await expect(load(event)).rejects.toMatchObject({ status: 400 });
    expect(event.fetch).not.toHaveBeenCalled();
  });

  test.each([
    { state: "granted" },
    { state: "granted", environment: "prod", grant: "codex-cluster" },
    { state: "granted", environment: "dev", grant: "another-account" },
    { state: "unexpected", environment: "dev", grant: "codex-cluster" },
  ])("does not treat mismatched dev evidence as a connection", async (body) => {
    expect(await load(request("?environment=dev", body))).toEqual({
      environment: "dev",
      state: "unavailable",
    });
  });

  test("keeps dev selected when the status endpoint fails", async () => {
    const event = request("?environment=dev");
    event.fetch.mockResolvedValue(new Response("unavailable", { status: 502 }));

    expect(await load(event)).toEqual({
      environment: "dev",
      state: "unavailable",
    });
    expect(event.fetch).toHaveBeenCalledOnce();
  });
});
