import { readFileSync } from "node:fs";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import posts from "virtual:blog/index";
import type { PostSummary } from "./blog-types";
import { BlogPage } from "./blog-page";
import { render as prerender } from "./prerender";
import { Root } from "./root";

const manifest: (PostSummary & { content: string })[] = JSON.parse(
  readFileSync(
    "../../monolith/frontend/src/lib/public/posts/posts-manifest.json",
    "utf8",
  ),
);

describe("blog index", () => {
  it("ships only the public summary fields, without markdown or recordings", () => {
    expect(posts).toEqual(
      manifest.map(({ slug, title, summary, date, tags }) => ({
        slug,
        title,
        summary,
        date,
        tags,
      })),
    );
    expect(JSON.stringify(posts)).not.toContain(manifest[0]!.content);
  });

  it("pre-renders every manifest summary and links to the existing live posts", () => {
    const container = document.createElement("div");
    container.innerHTML = prerender("blog.html");
    expect(container.querySelector("h1")!.textContent).toBe("Blog");
    const entries = [...container.querySelectorAll("article")];
    expect(entries).toHaveLength(manifest.length);
    for (const [index, post] of manifest.entries()) {
      const article = entries[index]!;
      const link = article.querySelector("h2 a")!;
      expect(link.textContent).toBe(post.title);
      // The preview host does not serve post pages in this slice.
      expect(link.getAttribute("href")).toBe(
        `https://jomcgi.dev/blog/${post.slug}`,
      );
      expect(article.querySelector("p")!.textContent).toBe(post.summary);
      expect(article.querySelector("time")!.getAttribute("datetime")).toBe(
        post.date,
      );
      expect(
        [...article.querySelectorAll('ul[aria-label="Tags"] a')].map((tag) => [
          tag.textContent,
          tag.getAttribute("href"),
        ]),
      ).toEqual(post.tags.map((tag) => [tag, `/blog?tag=${tag}`]));
    }
    expect(container.querySelector("[style]")).toBeNull();
  });

  it("keeps the live index preview tags", () => {
    const doc = new DOMParser().parseFromString(
      readFileSync("blog.html", "utf8"),
      "text/html",
    );
    const title = "Blog · jomcgi.dev";
    const description = "Technical posts by Joe McGinley.";
    expect(doc.title).toBe(title);
    expect(
      doc.querySelector('link[rel="canonical"]')!.getAttribute("href"),
    ).toBe("https://jomcgi.dev/blog");
    for (const [name, value] of Object.entries({
      description,
      "og:type": "website",
      "og:site_name": "Joe McGinley",
      "og:title": title,
      "og:description": description,
      "og:url": "https://jomcgi.dev/blog",
      "twitter:card": "summary",
      "twitter:title": title,
      "twitter:description": description,
    })) {
      expect(
        doc
          .querySelector(
            `meta[${name.startsWith("og:") ? "property" : "name"}="${name}"]`,
          )!
          .getAttribute("content"),
      ).toBe(value);
    }
  });
});

describe("tag filter", () => {
  it("reads ?tag= after hydration, filters and follows browser history", () => {
    window.history.replaceState(null, "", "/blog?tag=tla");
    render(
      <Root>
        <BlogPage />
      </Root>,
    );
    const entries = screen.getByRole("region", { name: "Blog posts" });
    expect(within(entries).getAllByRole("article")).toHaveLength(1);
    expect(entries.textContent).toContain("Formal verification at home");
    fireEvent.click(screen.getByRole("link", { name: "inference (1)" }));
    expect(window.location.search).toBe("?tag=inference");
    expect(entries.textContent).toContain("How to serve 134 GB");
    expect(
      screen
        .getByRole("link", { name: "inference (1)" })
        .getAttribute("aria-current"),
    ).toBe("page");
    window.history.replaceState(null, "", "/blog");
    fireEvent.popState(window);
    expect(within(entries).getAllByRole("article")).toHaveLength(
      manifest.length,
    );
  });

  it("ignores malformed tags and shows an empty state for unknown valid tags", () => {
    window.history.replaceState(null, "", "/blog?tag=%3Cbad%3E");
    render(
      <Root>
        <BlogPage />
      </Root>,
    );
    expect(screen.getAllByRole("article")).toHaveLength(manifest.length);
    window.history.replaceState(null, "", "/blog?tag=unknown");
    fireEvent.popState(window);
    expect(screen.getByText("Nothing published yet.")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("link", { name: `All (${manifest.length})` }),
    );
    expect(window.location.search).toBe("");
    expect(screen.getAllByRole("article")).toHaveLength(manifest.length);
  });
});
