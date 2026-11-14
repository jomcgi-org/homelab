import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { data, now } from "../../../../../test/hikes/fixtures.js";
import { ssr } from "./+page.js";

describe("/app/hikes SSR", () => {
  it("opts into server rendering", () => {
    expect(ssr).toBe(true);
  });

  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: { data: { snapshot: { walks: [], maxima: null } } },
    });

    expect(html).toContain("Hike planner, Scotland walks by weather window");
    expect(html.replace(/\s+/g, " ")).toContain(
      "Scottish hill walks filtered by duration, distance, and ascent, with the days the weather allows.",
    );
  });

  it("renders actual snapshot rows without a browser fetch", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    try {
      const { html } = await render(Page, {
        props: { data: { ...data, renderedAt: Date.parse(now) } },
      });
      expect(html).toContain("Walks with a weather window");
      expect(html).toContain("Fixture walk 1");
      expect(html).toContain("8 walks");
      expect(html.match(/<li class="snapshot-row/g)).toHaveLength(6);
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      fetch.mockRestore();
    }
  });

  it("uses the serialized clock even when a cached response is rendered later", async () => {
    const clock = vi
      .spyOn(Date, "now")
      .mockReturnValue(Date.parse(now) + 86400000);
    try {
      const { html } = await render(Page, {
        props: { data: { ...data, renderedAt: Date.parse(now) } },
      });
      expect(html.replace(/\s+/g, " ")).toContain("8 viable today");
    } finally {
      clock.mockRestore();
    }
  });
});
