import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
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
});
