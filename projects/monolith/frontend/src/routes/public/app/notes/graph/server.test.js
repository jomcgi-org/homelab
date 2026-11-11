import { beforeEach, describe, it, expect, vi } from "vitest";
import { GET } from "./+server.js";
import { readPublicSnapshot } from "../../../../../lib/server/public-snapshots.js";
vi.mock("../../../../../lib/server/public-snapshots.js", () => ({
  readPublicSnapshot: vi.fn(),
}));
beforeEach(() => vi.clearAllMocks());
import { GET as search } from "../../../slop/factory/search/+server.js";
import { GET as index } from "../../../slop/factory/search-index/+server.js";
import { GET as entities } from "../../../slop/factory/entities/+server.js";
import { GET as notes } from "../../../slop/factory/entities/[kind]/[slug]/notes/+server.js";
import { GET as body } from "../body/[id]/+server.js";

describe("public fact proxies", () => {
  it.each([search, index, entities, notes, body])(
    "forwards bounded origin policy and validators",
    async (handler) => {
      const setHeaders = vi.fn();
      const fetch = vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers({
          "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
          etag: '"facts"',
        }),
        json: async () => ({ notes: [] }),
      });
      await handler({
        fetch,
        setHeaders,
        url: new URL("https://jomcgi.dev/slop/factory/search?q=fact"),
        params: { id: "fact", kind: "project", slug: "monolith" },
      });
      expect(setHeaders).toHaveBeenCalledWith({
        "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
        "cloudflare-cdn-cache-control": "public, max-age=13, must-revalidate",
        etag: '"testbuild-facts"',
      });
    },
  );

  it.each([search, index, entities, notes, body])(
    "does not cache an exhausted or missing origin policy",
    async (handler) => {
      const setHeaders = vi.fn();
      const fetch = vi.fn().mockResolvedValue({
        ok: true,
        headers: new Headers({ "cache-control": "no-store" }),
        json: async () => [],
      });
      await handler({
        fetch,
        setHeaders,
        url: new URL("https://jomcgi.dev/slop/factory/search?q=fact"),
        params: { id: "fact", kind: "project", slug: "monolith" },
      });
      expect(setHeaders).toHaveBeenCalledWith({
        "cache-control": "no-store",
        "cloudflare-cdn-cache-control": "no-store",
      });
    },
  );
});

describe("graph public snapshot proxy", () => {
  it("serves the fixed public projection with its remaining lease", async () => {
    const headers = {
      "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
      etag: '\"testbuild-data\"',
    };
    readPublicSnapshot.mockResolvedValue({ body: '{"safe":true}', headers });
    const setHeaders = vi.fn();
    const response = await GET({ setHeaders });
    expect(readPublicSnapshot).toHaveBeenCalledWith("graph");
    expect(setHeaders).toHaveBeenCalledWith(headers);
    expect(await response.json()).toEqual({ safe: true });
  });
  it("makes a failed refresh uncacheable", async () => {
    readPublicSnapshot.mockRejectedValue(new Error("backend unavailable"));
    const setHeaders = vi.fn();
    await expect(GET({ setHeaders })).rejects.toMatchObject({ status: 503 });
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "no-store",
      "cloudflare-cdn-cache-control": "no-store",
    });
  });
});
