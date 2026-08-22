import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";

describe("/app/ships SSR", () => {
  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: { data: { snapshot: { vessels: [] } } },
    });

    expect(html).toContain("Live ships, AIS vessel tracker");
    expect(html).toContain("Real-time tracking of vessels using AIS data.");
  });
});
