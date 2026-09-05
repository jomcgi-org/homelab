import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";

async function renderPage(status) {
  const { html } = await render(Page, {
    props: { data: { status, savings: null } },
  });
  return html.replace(/ class="[^"]*"/g, "").replace(/\s+/g, " ");
}

describe("/ember status copy", () => {
  it.each([null, { configured: false }])(
    "keeps unknown status neutral for %j",
    async (status) => {
      const html = await renderPage(status);
      expect(html).toContain("the demo Postgres is <b>asleep</b> right now");
      expect(html).not.toContain("<b>offline</b>");
      expect(html).toContain("asleep now");
    },
  );

  it.each(["failed", "evicted", "destroying", "destroyed"])(
    "reserves offline for terminal state %s",
    async (state) => {
      const html = await renderPage({ configured: true, state });
      expect(html).toContain("the demo Postgres is <b>offline</b> right now");
    },
  );

  it("keeps a serving draining machine awake", async () => {
    const html = await renderPage({
      configured: true,
      state: "serving",
      preempted: { phase: "confirming" },
      display_preempted: false,
    });
    expect(html).toContain("the demo Postgres is <b>awake</b> right now");
    expect(html).not.toContain("shut down at short notice");
  });
});
