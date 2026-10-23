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
});
