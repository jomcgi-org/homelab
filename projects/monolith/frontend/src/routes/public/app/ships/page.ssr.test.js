import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { ssr } from "./+page.js";

describe("/app/ships SSR", () => {
  it("opts into server rendering", () => {
    expect(ssr).toBe(true);
  });

  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: { data: { snapshot: { vessels: [] } } },
    });

    expect(html).toContain("Live ships, AIS vessel tracker");
    expect(html.replace(/\s+/g, " ")).toContain(
      "Live vessel positions from AIS, the position broadcasts ships transmit.",
    );
  });
});
