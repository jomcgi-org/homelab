import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { ssr } from "./+page.js";

describe("/app/notes SSR", () => {
  it("opts into server rendering", () => {
    expect(ssr).toBe(true);
  });

  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: {
        data: {
          turnstileSiteKey: "test",
          stats: null,
          admitted: false,
          initialMessages: [],
        },
      },
    });

    expect(html).toContain("Chat with my knowledge graph");
    expect(html.replace(/\s+/g, " ")).toContain(
      "Ask questions of my public notes; every answer cites the notes it came from.",
    );
  });
});
