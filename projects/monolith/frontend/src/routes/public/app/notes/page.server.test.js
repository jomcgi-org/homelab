import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";
import { readPublicSnapshot } from "../../../../lib/server/public-snapshots.js";

vi.mock("../../../../lib/server/public-snapshots.js", () => ({
  readPublicSnapshot: vi.fn(),
}));

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
  vi.clearAllMocks();
});
afterEach(() => vi.useRealTimers());

const headers = {
  "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
  "cloudflare-cdn-cache-control": "public, max-age=13, must-revalidate",
  date: new Date(1_000_000).toUTCString(),
  age: "3",
  etag: '"build-graph"',
  "last-modified": new Date(900_000).toUTCString(),
};

describe("/app/notes first view", () => {
  it("projects eight real titles without shipping the full graph", async () => {
    const nodes = Array.from({ length: 12 }, (_, i) => ({
      id: `note-${i}`,
      title: `Public title ${i}`,
      degree: i,
      x: i / 12,
    }));
    readPublicSnapshot.mockResolvedValue({
      body: JSON.stringify({
        nodes,
        edges: [{ source: "note-1", target: "note-2" }],
      }),
      headers,
    });
    const setHeaders = vi.fn();
    const data = await load({ setHeaders });
    expect(readPublicSnapshot).toHaveBeenCalledExactlyOnceWith("graph");
    expect(data.overview).toEqual({
      nodeCount: 12,
      edgeCount: 1,
      titles: nodes
        .slice(4)
        .reverse()
        .map(({ id, title }) => ({ id, title })),
      expiresAt: 1_010_000,
    });
    const { etag, "last-modified": modified, ...pageHeaders } = headers;
    expect(setHeaders).toHaveBeenCalledExactlyOnceWith(pageHeaders);
    expect(JSON.stringify(data)).not.toContain('"edges"');
  });

  it("lets HTML validators change when the same graph receives a new lease", async () => {
    const body = JSON.stringify({
      nodes: [{ id: "note", title: "Still current" }],
      edges: [],
    });
    const pages = [];
    for (const now of [1_000_000, 1_060_000]) {
      vi.setSystemTime(now);
      readPublicSnapshot.mockResolvedValue({
        body,
        headers: { ...headers, date: new Date(now).toUTCString() },
      });
      const setHeaders = vi.fn();
      pages.push(await load({ setHeaders }));
      // Kit computes the page ETag from its rendered body, which includes
      // the new deadline. Graph validators cannot force that body to a 304.
      expect(setHeaders.mock.calls[0][0]).not.toHaveProperty("etag");
      expect(setHeaders.mock.calls[0][0]).not.toHaveProperty("last-modified");
    }
    expect(pages[0].overview.titles).toEqual(pages[1].overview.titles);
    expect(pages[1].overview.expiresAt - pages[0].overview.expiresAt).toBe(
      60_000,
    );
    expect(JSON.stringify(pages[0])).not.toBe(JSON.stringify(pages[1]));
  });

  it.each(["no-store", "public, max-age=0", "private, max-age=60"])(
    "does not serialize titles outside a cache lease: %s",
    async (policy) => {
      readPublicSnapshot.mockResolvedValue({
        body: JSON.stringify({
          nodes: [{ id: "expired", title: "Expired title" }],
        }),
        headers: { "cache-control": policy },
      });
      const setHeaders = vi.fn();
      expect(await load({ setHeaders })).toEqual({ overview: null });
      expect(setHeaders).toHaveBeenCalledExactlyOnceWith({
        "cache-control": "no-store",
        "cloudflare-cdn-cache-control": "no-store",
      });
    },
  );

  it("consumes snapshot age and rejects expiry at equality", async () => {
    readPublicSnapshot.mockResolvedValue({
      body: '{"nodes":[],"edges":[]}',
      headers: { ...headers, age: "13" },
    });
    expect(await load({ setHeaders: vi.fn() })).toEqual({ overview: null });
  });

  it.each(["unavailable", "malformed"])(
    "fails closed when %s",
    async (failure) => {
      if (failure === "unavailable")
        readPublicSnapshot.mockRejectedValue(new Error("offline"));
      else readPublicSnapshot.mockResolvedValue({ body: "not JSON", headers });
      const setHeaders = vi.fn();
      expect(await load({ setHeaders })).toEqual({ overview: null });
      expect(setHeaders).toHaveBeenCalledExactlyOnceWith({
        "cache-control": "no-store",
        "cloudflare-cdn-cache-control": "no-store",
      });
    },
  );
});
