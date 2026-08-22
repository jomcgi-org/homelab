import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";

describe("/app/notes SSR", () => {
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
    expect(html).toContain(
      "Chat with my public knowledge graph, or switch to the graph view to browse it.",
    );
  });
});
