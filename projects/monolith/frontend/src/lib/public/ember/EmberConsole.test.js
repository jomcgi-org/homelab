import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import EmberConsole from "./EmberConsole.svelte";

async function renderConsole(initialStatus) {
  const { html } = await render(EmberConsole, {
    props: { initialStatus, initialSavings: null },
  });
  return html.replace(/\s+/g, " ");
}

describe("EmberConsole preemption display", () => {
  it("does not repeat the preemption sentence already shown by the hero", async () => {
    const html = await renderConsole({
      configured: true,
      state: "failed",
      preempted: { phase: "restoring" },
      display_preempted: true,
    });

    expect(html).not.toContain("shut down at short notice");
    expect(html).not.toContain("status: The machine");
  });

  it("does not call a serving drain rehoming", async () => {
    const html = await renderConsole({
      configured: true,
      state: "serving",
      preempted: { phase: "confirming" },
      display_preempted: false,
    });

    expect(html).not.toContain("rehoming");
    expect(html).not.toContain("shut down at short notice");
  });
});
