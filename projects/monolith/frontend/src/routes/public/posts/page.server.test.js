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

describe("/public/posts load", () => {
  beforeEach(() => {
    manifest.splice(0);
  });

  it("returns an empty manifest", () => {
    const setHeaders = vi.fn();

    const result = load({ setHeaders });

    expect(result.posts).toEqual([]);
    expect(setHeaders).toHaveBeenCalledWith({
      ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
      etag: '"testbuild-posts"',
    });
  });

  it("lists a published entry without returning its body", () => {
    manifest.push({
      path: "docs/posts/2026-01-15-example.md",
      slug: "example",
      title: "Example",
      date: "2026-01-15",
      summary: "One sentence.",
      content: "Secret from the index payload.",
    });

    const result = load({ setHeaders: vi.fn() });

    expect(result.posts).toEqual([
      {
        slug: "example",
        title: "Example",
        date: "2026-01-15",
        summary: "One sentence.",
      },
    ]);
    expect(result.posts[0]).not.toHaveProperty("content");
  });
});
