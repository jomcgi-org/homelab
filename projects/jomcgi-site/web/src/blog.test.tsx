import { readFileSync } from "node:fs";
import { act } from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import type { PostSummary } from "./blog-types";
const manifest: (PostSummary & { content: string })[] = JSON.parse(
  readFileSync(
    "../../monolith/frontend/src/lib/public/posts/posts-manifest.json",
    "utf8",
  ),
);
import { BlogPage } from "./blog-page";
import { BlogEmbed, type EmbedKind } from "./blog-embed";
import { PAGES } from "./pages";
import { render as prerender } from "./prerender";
import { Root } from "./root";

const page = (element: React.ReactNode) => render(<Root>{element}</Root>);

describe("blog content contract", () => {
  it("keeps each recording identical to the public hub recording", () => {
    for (const name of ["conformance-replay", "qwen-replay", "agent-replay"]) {
      expect(
        JSON.parse(readFileSync(`src/embeds/${name}.json`, "utf8")),
      ).toEqual(
        JSON.parse(
          readFileSync(
            `../../monolith/frontend/src/lib/public/posts/${name}.json`,
            "utf8",
          ),
        ),
      );
    }
  });
  it("registers every manifest slug and pre-renders its title and sections", () => {
    for (const post of manifest) {
      const entry = `blog/${post.slug}.html`;
      expect(PAGES[entry], post.slug).toBeDefined();
      const container = document.createElement("div");
      container.innerHTML = prerender(entry);
      expect(container.querySelector("h1")!.textContent).toBe(post.title);
      expect(container.textContent).toContain(post.summary);
      for (const tag of post.tags)
        expect(
          container.querySelector(`a[href="/blog?tag=${tag}"]`),
        ).toBeTruthy();
      const headings = [...container.querySelectorAll("h2, h3")].map(
        (h) => h.textContent,
      );
      for (const [, heading] of post.content.matchAll(/^#{2,3} (.+)$/gm))
        expect(headings).toContain(heading);
      expect(container.querySelector("[style]")).toBeNull();
      expect(
        container.querySelector(".blog-prose")!.textContent!.length,
      ).toBeGreaterThan(300);
      for (const link of container.querySelectorAll(
        'nav[aria-label="Sections"] a',
      ))
        expect(
          container.querySelector(
            `[id="${link.getAttribute("href")!.slice(1)}"]`,
          ),
        ).toBeTruthy();
    }
  });

  it("keeps the live preview tags on the index and every post", () => {
    for (const post of [
      { slug: "", title: "Blog", summary: "Technical posts by Joe McGinley." },
      ...manifest,
    ]) {
      const path = `blog${post.slug ? `/${post.slug}` : ""}`;
      const doc = new DOMParser().parseFromString(
        readFileSync(`${path}.html`, "utf8"),
        "text/html",
      );
      const title = `${post.title} · jomcgi.dev`;
      expect(doc.title.replace(/\s+/g, " ").trim()).toBe(title);
      expect(
        doc.querySelector('link[rel="canonical"]')!.getAttribute("href"),
      ).toBe(`https://jomcgi.dev/${path}`);
      for (const [name, value] of Object.entries({
        description: post.summary,
        "og:type": post.slug ? "article" : "website",
        "og:site_name": "Joe McGinley",
        "og:title": title,
        "og:description": post.summary,
        "og:url": `https://jomcgi.dev/${path}`,
        "twitter:card": "summary",
        "twitter:title": title,
        "twitter:description": post.summary,
      }))
        expect(
          doc
            .querySelector(
              `meta[${name.startsWith("og:") ? "property" : "name"}="${name}"]`,
            )!
            .getAttribute("content"),
        ).toBe(value);
    }
  });

  it("includes every original static figure without JavaScript or inline styles", () => {
    const container = document.createElement("div");
    container.innerHTML = prerender("blog/125b-on-a-4090.html");
    expect(container.querySelectorAll("figure.ix")).toHaveLength(9);
    for (const text of [
      "134.26 GB",
      "VRAM 24 GB",
      "1,969 records",
      "All at once",
      "92–95% kept",
      "KL 0.10–0.13",
      "0.64 s",
      "Total, 8 streams",
      "179.4–192.7 tok/s",
    ])
      expect(container.textContent).toContain(text);
    expect(container.querySelectorAll(".ix-slot")).toHaveLength(0);
    expect(container.querySelectorAll(".fig-art svg")).toHaveLength(1);
  });
});

describe("tag filter", () => {
  it("reads ?tag= after hydration, filters and follows browser history", async () => {
    window.history.replaceState(null, "", "/blog?tag=tla");
    page(<BlogPage />);
    const posts = screen.getByRole("region", { name: "Blog posts" });
    expect(within(posts).getAllByRole("article")).toHaveLength(1);
    expect(posts.textContent).toContain("Formal verification at home");
    fireEvent.click(screen.getByRole("link", { name: "inference (1)" }));
    expect(window.location.search).toBe("?tag=inference");
    expect(posts.textContent).toContain("How to serve 134 GB");
    window.history.replaceState(null, "", "/blog");
    fireEvent.popState(window);
    expect(within(posts).getAllByRole("article")).toHaveLength(manifest.length);
  });
  it("ignores malformed tags and shows an empty state for unknown valid tags", () => {
    window.history.replaceState(null, "", "/blog?tag=%3Cbad%3E");
    page(<BlogPage />);
    expect(screen.getAllByRole("article")).toHaveLength(manifest.length);
    window.history.replaceState(null, "", "/blog?tag=unknown");
    fireEvent.popState(window);
    expect(screen.getByText("Nothing published yet.")).toBeTruthy();
    window.history.replaceState(null, "", "/blog");
  });
});

describe("replay intent", () => {
  it("keeps landmarks distinct when both inference demos are loaded", async () => {
    const { container } = page(
      <>
        <BlogEmbed kind="research" label="Research" />
        <BlogEmbed kind="coding" label="Coding" />
      </>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Load Research" }));
    fireEvent.click(screen.getByRole("button", { name: "Load Coding" }));
    await waitFor(() => expect(screen.getAllByRole("slider")).toHaveLength(2), {
      timeout: 10000,
    });
    const { tick } = await import("svelte");
    await act(async () => {
      for (const slider of screen.getAllByRole("slider") as HTMLInputElement[])
        fireEvent.input(slider, { target: { value: slider.max } });
      await tick();
    });
    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  }, 20000);
  it("does not load until activation, disposes on unmount and offers retry", async () => {
    const dispose = vi.fn();
    const loader = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(dispose);
    const view = page(
      <BlogEmbed kind="coding" label="Coding demo" loader={loader} />,
    );
    expect(loader).not.toHaveBeenCalled();
    fireEvent.focus(screen.getByRole("button"));
    fireEvent.mouseOver(screen.getByRole("button"));
    expect(loader).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button"));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Retry Coding demo" }));
    await waitFor(() => expect(screen.queryByRole("button")).toBeNull());
    expect(loader).toHaveBeenCalledTimes(2);
    view.unmount();
    expect(dispose).toHaveBeenCalledOnce();
  });

  it.each(["conformance", "research", "coding"] as EmbedKind[])(
    "%s plays, scrubs and remains accessible",
    async (kind) => {
      const { container } = page(<BlogEmbed kind={kind} label={kind} />);
      fireEvent.click(screen.getByRole("button", { name: `Load ${kind}` }));
      const slider = (await screen.findByRole(
        "slider",
        {},
        { timeout: 10000 },
      )) as HTMLInputElement;
      const { tick } = await import("svelte");
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /^Play$/ }));
        await tick();
      });
      expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();
      await act(async () => {
        fireEvent.input(slider, { target: { value: slider.max } });
        await tick();
      });
      expect(screen.getByRole("button", { name: "Replay" })).toBeTruthy();
      if (kind === "conformance") {
        expect(
          container.querySelectorAll('.cell[data-verdict="pass"]'),
        ).toHaveLength(6);
        await act(async () => {
          fireEvent.click(
            screen.getByRole("button", { name: /No destroy before confirm/ }),
          );
          await tick();
        });
        expect(container.querySelector(".why")!.textContent).toContain(
          "after the node confirmed",
        );
      } else if (kind === "research") {
        const node = container.querySelector<HTMLButtonElement>(".graph-node")!;
        expect(node).toBeTruthy();
        await act(async () => {
          fireEvent.click(node);
          await tick();
        });
        expect(node.getAttribute("aria-pressed")).toBe("true");
      } else {
        expect(container.querySelector("figure.trace")).toBeTruthy();
      }
      expect(container.querySelector("[style]")).toBeNull();
      const result = await axe.run(container, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(
        result.violations.map((v) => ({
          id: v.id,
          nodes: v.nodes.map((n) => n.target),
        })),
      ).toEqual([]);
    },
    20000,
  );
});
