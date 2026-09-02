import { beforeEach, describe, expect, it, vi } from "vitest";

const { manifest } = vi.hoisted(() => ({ manifest: [] }));

vi.mock("$lib/public/posts/posts-manifest.json", () => ({
  default: manifest,
}));

import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";
import { load } from "./+page.server.js";

describe("/public/blog load", () => {
  beforeEach(() => {
    manifest.splice(0);
  });

  it("returns an empty manifest", () => {
    const setHeaders = vi.fn();

    const result = load({
      setHeaders,
      url: new URL("https://jomcgi.dev/blog"),
    });

    expect(result.posts).toEqual([]);
    expect(result.tags).toEqual([]);
    expect(result.months).toEqual([]);
    expect(result.selectedTag).toBe("");
    expect(setHeaders).toHaveBeenCalledWith({
      ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
      etag: '"testbuild-blog-all"',
    });
  });

  it("lists a published entry without returning its body", () => {
    manifest.push({
      path: "docs/posts/2026-01-15-example.md",
      slug: "example",
      title: "Example",
      date: "2026-01-15",
      summary: "One sentence.",
      tags: ["inference", "moe"],
      content: "Secret from the index payload.",
    });

    const result = load({
      setHeaders: vi.fn(),
      url: new URL("https://jomcgi.dev/blog?tag=moe"),
    });

    expect(result.posts).toEqual([
      {
        slug: "example",
        title: "Example",
        date: "2026-01-15",
        summary: "One sentence.",
        tags: ["inference", "moe"],
      },
    ]);
    expect(result.tags).toEqual([
      { name: "inference", count: 1 },
      { name: "moe", count: 1 },
    ]);
    expect(result.months[0].posts).toEqual(result.posts);
    expect(result.selectedTag).toBe("moe");
    expect(result.posts[0]).not.toHaveProperty("content");
  });

  it("returns an empty result for an unknown tag", () => {
    manifest.push({
      slug: "example",
      title: "Example",
      date: "2026-01-15",
      summary: "One sentence.",
      tags: ["known"],
      content: "Body.",
    });

    const result = load({
      setHeaders: vi.fn(),
      url: new URL("https://jomcgi.dev/blog?tag=unknown"),
    });

    expect(result.posts).toEqual([]);
    expect(result.selectedTag).toBe("unknown");
  });
});
