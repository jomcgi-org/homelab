import { describe, expect, it } from "vitest";
import { gunzipSync } from "node:zlib";
import { handle, pickEncoding } from "./hooks.server.js";

describe("response compression", () => {
  it.each([
    [null, null],
    ["identity", null],
    ["gzip;q=1, br;q=0", "gzip"],
    ["gzip;q=0, identity;q=1", null],
    ["gzip;q=1, br;q=0.1", "gzip"],
    ["gzip, br", "br"],
    ["BR; q=0.8, gzip;q=0.5", "br"],
    ["*;q=0.5, br;q=0", "gzip"],
    ["*;q=0", null],
    ["gzip;q=0.5, identity;q=1", null],
    ["gzip;q=invalid", null],
    ["gzip;q=2", null],
  ])("negotiates %s as %s", (header, expected) => {
    expect(pickEncoding(header)).toBe(expected);
  });
  it("returns decodable gzip when Brotli is rejected", async () => {
    const body = "response ".repeat(300);
    const response = await handle({
      event: {
        request: new Request("https://example.com", {
          headers: { "Accept-Encoding": "gzip;q=1, br;q=0" },
        }),
      },
      resolve: async () =>
        new Response(body, { headers: { "Content-Type": "text/plain" } }),
    });
    expect(response.headers.get("content-encoding")).toBe("gzip");
    expect(response.headers.get("vary")).toContain("Accept-Encoding");
    expect(
      gunzipSync(Buffer.from(await response.arrayBuffer())).toString(),
    ).toBe(body);
  });
  it("leaves the body readable when compression is rejected", async () => {
    const body = "response ".repeat(300);
    const response = await handle({
      event: {
        request: new Request("https://example.com", {
          headers: { "Accept-Encoding": "gzip;q=0, br;q=0" },
        }),
      },
      resolve: async () =>
        new Response(body, { headers: { "Content-Type": "text/plain" } }),
    });
    expect(response.headers.has("content-encoding")).toBe(false);
    expect(await response.text()).toBe(body);
  });
});
