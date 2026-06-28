import { describe, it, expect, vi, afterEach } from "vitest";
import { GET } from "./+server.js";

const CSP_FALLBACK =
  "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("/public/artifact/[id]/raw GET", () => {
  it("proxies to the correct backend URL", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => null },
      text: async () => "<h1>hello</h1>",
    });
    vi.stubGlobal("fetch", fetch);

    await GET({ params: { id: "abc123" }, fetch });

    expect(fetch).toHaveBeenCalledTimes(1);
    const url = fetch.mock.calls[0][0];
    expect(url).toMatch(/\/internal\/artifact\/abc123\/raw$/);
  });

  it("URL-encodes the artifact id in the backend request", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => null },
      text: async () => "<p>ok</p>",
    });
    vi.stubGlobal("fetch", fetch);

    await GET({ params: { id: "hello world/slash" }, fetch });

    const url = fetch.mock.calls[0][0];
    expect(url).toContain(encodeURIComponent("hello world/slash"));
    expect(url).not.toContain(" ");
  });

  it("returns the HTML body from the backend", async () => {
    const html = "<html><body>artifact content</body></html>";
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => null },
      text: async () => html,
    });
    vi.stubGlobal("fetch", fetch);

    const res = await GET({ params: { id: "abc123" }, fetch });

    expect(await res.text()).toBe(html);
    expect(res.headers.get("content-type")).toBe("text/html; charset=utf-8");
  });

  it("sets cache-control: no-store", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => null },
      text: async () => "<p>ok</p>",
    });
    vi.stubGlobal("fetch", fetch);

    const res = await GET({ params: { id: "x" }, fetch });

    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  it("forwards the CSP header from the backend when present", async () => {
    const backendCsp = "default-src 'none'; script-src 'unsafe-inline'";
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: {
        get: (h) => (h === "content-security-policy" ? backendCsp : null),
      },
      text: async () => "<p>ok</p>",
    });
    vi.stubGlobal("fetch", fetch);

    const res = await GET({ params: { id: "x" }, fetch });

    expect(res.headers.get("content-security-policy")).toBe(backendCsp);
  });

  it("uses the fallback CSP when the backend does not set one", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => null },
      text: async () => "<p>ok</p>",
    });
    vi.stubGlobal("fetch", fetch);

    const res = await GET({ params: { id: "x" }, fetch });

    expect(res.headers.get("content-security-policy")).toBe(CSP_FALLBACK);
  });

  it("throws a 404 error when the backend returns 404", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal("fetch", fetch);

    await expect(
      GET({ params: { id: "missing" }, fetch }),
    ).rejects.toMatchObject({ status: 404 });
  });

  it("throws a 503 error when the backend returns a non-404 error", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 502 });
    vi.stubGlobal("fetch", fetch);

    await expect(GET({ params: { id: "x" }, fetch })).rejects.toMatchObject({
      status: 503,
    });
  });
});
