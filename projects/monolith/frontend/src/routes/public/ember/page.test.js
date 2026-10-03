import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";

describe("/ember recorded demo", () => {
  it("links to recorded restores without live database status", async () => {
    const { html } = await render(Page);
    expect(html).toContain("/ember/firecracker#replay");
    expect(html).not.toContain("right now");
    expect(html).not.toContain("/ember/postgres");
  });
});
