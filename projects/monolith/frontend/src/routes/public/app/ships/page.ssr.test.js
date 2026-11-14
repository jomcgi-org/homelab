import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { snapshot } from "../../../../../test/ships-fixtures.js";
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

  it("renders actual snapshot rows without a browser fetch", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    try {
      const { html } = await render(Page, { props: { data: { snapshot } } });
      expect(html).toContain("Vessel snapshot");
      expect(html).toContain("Fixture vessel 1");
      expect(html).toContain("48 vessels");
      expect(html.match(/<li class="snapshot-row/g)).toHaveLength(6);
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      fetch.mockRestore();
    }
  });
});
