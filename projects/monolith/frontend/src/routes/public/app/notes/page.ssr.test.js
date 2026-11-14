import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { ssr } from "./+page.js";

describe("/app/notes SSR", () => {
  it("opts into server rendering", () => {
    expect(ssr).toBe(true);
  });

  it("renders the sheet shell and metadata without mounting the graph", async () => {
    const { html, head } = await render(Page);

    expect(html).toContain("Public notes");
    expect(html.replace(/\s+/g, " ")).toContain("Watch the factory");
    expect(html).toContain("td sheet notes-page");
    expect(html).not.toContain("<canvas");
    expect(html).not.toContain("loading the graph");
    expect(head).toContain("Public notes · jomcgi.dev");
    expect(head).toContain(
      "Browse my public knowledge graph and the notes behind the factory.",
    );
  });

  it("renders real counts and readable titles before any client code or fetch", async () => {
    const { html } = await render(Page, {
      props: {
        data: {
          overview: {
            nodeCount: 17,
            edgeCount: 24,
            titles: [{ id: "note / one", title: "A current public note" }],
            expiresAt: Date.now() + 60_000,
          },
        },
      },
    });
    expect(html).toContain("17 notes");
    expect(html).toContain("24 links");
    expect(html).toContain("A current public note");
    expect(html).toContain("?focus=note%20%2F%20one");
    expect(html).not.toContain("<canvas");
    expect(html).not.toContain("loading the graph");
  });

  it("does not render an already expired overview", async () => {
    const { html } = await render(Page, {
      props: {
        data: {
          overview: {
            nodeCount: 1,
            edgeCount: 0,
            titles: [{ id: "expired", title: "Expired fact" }],
            expiresAt: Date.now() - 1,
          },
        },
      },
    });
    expect(html).not.toContain("Expired fact");
    expect(html).toContain("No current snapshot is available.");
  });
});
