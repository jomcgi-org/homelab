import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";

describe("/app/stars SSR", () => {
  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: { data: { snapshot: { sites: [], count: 0 } } },
    });

    expect(html).toContain("Dark-sky stargazing map, Scotland viewing windows");
    expect(html.replace(/\s+/g, " ")).toContain(
      "Find ideal stargazing locations in Scotland by light pollution and viewing windows.",
    );
  });
});
