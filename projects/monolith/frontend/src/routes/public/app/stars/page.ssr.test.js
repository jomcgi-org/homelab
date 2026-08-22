import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { ssr } from "./+page.js";

describe("/app/stars SSR", () => {
  it("opts into server rendering", () => {
    expect(ssr).toBe(true);
  });

  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: { data: { snapshot: { sites: [], count: 0 } } },
    });

    expect(html).toContain("Dark-sky stargazing map, Scotland viewing windows");
    expect(html.replace(/\s+/g, " ")).toContain(
      "Scotland's dark-sky sites ranked by forecast clear, dark hours.",
    );
  });
});
