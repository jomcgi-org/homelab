import { describe, it, expect, vi, afterEach } from "vitest";
import { GET } from "./+server.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("/public/artifact/[id]/version GET", () => {
  it("proxies to the correct backend URL", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ version: "etag-abc" }),
    });
    vi.stubGlobal("fetch", fetch);

    await GET({ params: { id: "abc123" }, fetch });

    expect(fetch).toHaveBeenCalledTimes(1);
    const url = fetch.mock.calls[0][0];
    expect(url).toMatch(/\/internal\/artifact\/abc123\/version$/);
  });

  it("URL-encodes the artifact id in the backend request", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ version: "v1" }),
    });
    vi.stubGlobal("fetch", fetch);

    await GET({ params: { id: "hello world" }, fetch });

    const url = fetch.mock.calls[0][0];
    expect(url).toContain(encodeURIComponent("hello world"));
    expect(url).not.toContain(" ");
  });

  it("passes the JSON payload through from the backend", async () => {
    const payload = { version: "etag-deadbeef" };
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetch);

    const res = await GET({ params: { id: "x" }, fetch });

    expect(await res.json()).toEqual(payload);
  });

  it("sets cache-control: no-store on the response", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ version: "v1" }),
    });
    vi.stubGlobal("fetch", fetch);

    const res = await GET({ params: { id: "x" }, fetch });

    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  it("throws a 404 error when the backend returns 404", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal("fetch", fetch);

    await expect(
      GET({ params: { id: "missing" }, fetch }),
    ).rejects.toMatchObject({ status: 404 });
  });

  it("throws a 503 error when the backend returns a non-404 error", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    vi.stubGlobal("fetch", fetch);

    await expect(GET({ params: { id: "x" }, fetch })).rejects.toMatchObject({
      status: 503,
    });
  });
});
