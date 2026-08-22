import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";

describe("/app/hikes SSR", () => {
  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: { data: { walks: [], maxima: null } },
    });

    expect(html).toContain("Hike planner, Scotland walks by weather window");
    expect(html).toContain(
      "Filter Scottish hill walks by distance, weather, and viewing location.",
    );
  });
});
