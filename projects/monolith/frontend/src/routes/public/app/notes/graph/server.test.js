import { describe, it, expect, vi } from "vitest";
import { GET } from "./+server.js";
import { GET as search } from "../../../slop/factory/search/+server.js";
import { GET as index } from "../../../slop/factory/search-index/+server.js";
import { GET as entities } from "../../../slop/factory/entities/+server.js";
import { GET as notes } from "../../../slop/factory/entities/[kind]/[slug]/notes/+server.js";
import { GET as facts } from "../../../slop/factory/facts/+server.js";
import { GET as body } from "../body/[id]/+server.js";

describe("public fact proxies", () => {
  it.each([GET, search, index, entities, notes, facts, body])(
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

  it.each([GET, search, index, entities, notes, facts, body])(
    "does not cache an exhausted or missing origin policy",
    async (handler) => {
      const setHeaders = vi.fn();
      const fetch = vi
        .fn()
        .mockResolvedValue({
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

function makeHeaders(map = {}) {
  const lower = Object.fromEntries(
    Object.entries(map).map(([k, v]) => [k.toLowerCase(), v]),
  );
  return { get: (name) => lower[name.toLowerCase()] ?? null };
}

describe("/public/app/notes/graph GET", () => {
  it("hits the visibility-filtered public graph endpoint", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => ({ nodes: [], edges: [], indexed_at: null }),
    });

    await GET({ fetch, setHeaders });

    expect(fetch).toHaveBeenCalledTimes(1);
    const url = fetch.mock.calls[0][0];
    expect(url).toMatch(/\/api\/knowledge\/public\/graph$/);
    // Belt-and-braces: must NEVER fall back to the unfiltered private endpoint,
    // which would leak private nodes onto public.jomcgi.dev.
    expect(url).not.toMatch(/\/api\/knowledge\/graph$/);
  });

  it("preserves the upstream deadline bound and returns the graph JSON", async () => {
    const setHeaders = vi.fn();
    const graph = { nodes: [], edges: [], indexed_at: null };
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        "cache-control": "public, max-age=20, s-maxage=20, must-revalidate",
      }),
      json: async () => graph,
    });

    const res = await GET({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        "cache-control": "public, max-age=20, s-maxage=20, must-revalidate",
        "cloudflare-cdn-cache-control": "public, max-age=20, must-revalidate",
      }),
    );
    expect(await res.json()).toEqual(graph);
  });

  it("versions the API ETag with the build version and forwards Last-Modified", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({
        ETag: '"abc-3"',
        "Last-Modified": "Mon, 27 Apr 2026 12:00:00 GMT",
      }),
      json: async () => ({ nodes: [], edges: [], indexed_at: null }),
    });

    await GET({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        // Build version (testbuild, from the $app/environment stub) is spliced
        // inside the quotes so a layout-only deploy busts the validator.
        etag: '"testbuild-abc-3"',
        "last-modified": "Mon, 27 Apr 2026 12:00:00 GMT",
      }),
    );
  });

  it("omits ETag and Last-Modified when the API does not return them", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => ({ nodes: [], edges: [], indexed_at: null }),
    });

    await GET({ fetch, setHeaders });

    const headers = setHeaders.mock.calls[0][0];
    expect(headers).not.toHaveProperty("etag");
    expect(headers).not.toHaveProperty("last-modified");
  });

  it("throws a 503 when the backend fetch fails", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 502 });

    await expect(GET({ fetch, setHeaders })).rejects.toThrow();
  });
});
