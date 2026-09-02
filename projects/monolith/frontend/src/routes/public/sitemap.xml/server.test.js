import { beforeEach, describe, expect, it, vi } from "vitest";

const { manifest } = vi.hoisted(() => ({ manifest: [] }));

vi.mock("$lib/public/posts/posts-manifest.json", () => ({
  default: manifest,
}));

import { GET } from "./+server.js";

describe("/public/sitemap.xml", () => {
  beforeEach(() => {
    manifest.splice(0);
  });

  it("omits the blog until a post is published", async () => {
    const body = await GET().text();

    expect(body).not.toContain("/blog");
    expect(body).toContain("https://jomcgi.dev/docs");
  });

  it("lists the blog index and every post once published", async () => {
    manifest.push({ slug: "first-post", date: "2026-09-01" });

    const body = await GET().text();

    expect(body).toContain("<loc>https://jomcgi.dev/blog</loc>");
    expect(body).toContain("<loc>https://jomcgi.dev/blog/first-post</loc>");
  });
});
