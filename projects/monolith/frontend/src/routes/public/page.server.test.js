import { describe, expect, it, vi } from "vitest";
import {
  cloudflareCacheHeaders,
  PAGE_CACHE_CONTROL,
} from "$lib/cache-headers.js";
import { _HOMEPAGE_POSTS, _latestPosts, load } from "./+page.server.js";

describe("public homepage load", () => {
  it("sets separate browser and Cloudflare cache policies and lists the latest posts", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ nodes: 4 }),
    });

    const result = await load({ fetch, setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      cloudflareCacheHeaders(PAGE_CACHE_CONTROL),
    );
    expect(result.stats).toEqual({ nodes: 4 });
    expect(Array.isArray(result.posts)).toBe(true);
    expect(result.posts.length).toBeLessThanOrEqual(_HOMEPAGE_POSTS);
    for (const post of result.posts) {
      expect(Object.keys(post).sort()).toEqual([
        "date",
        "slug",
        "summary",
        "title",
      ]);
      // Post bodies stay server-side: the homepage never ships them.
      expect(post.content).toBeUndefined();
    }
  });

  it("keeps the newest posts first and drops everything but the legend fields", () => {
    const posts = _latestPosts(
      [
        {
          slug: "a",
          title: "A",
          date: "2026-01-01",
          summary: "a",
          content: "body",
          tags: [],
        },
        {
          slug: "c",
          title: "C",
          date: "2026-03-01",
          summary: "c",
          content: "body",
          tags: [],
        },
        {
          slug: "b",
          title: "B",
          date: "2026-02-01",
          summary: "b",
          content: "body",
          tags: [],
        },
      ],
      2,
    );
    expect(posts).toEqual([
      { slug: "c", title: "C", date: "2026-03-01", summary: "c" },
      { slug: "b", title: "B", date: "2026-02-01", summary: "b" },
    ]);
  });
});
