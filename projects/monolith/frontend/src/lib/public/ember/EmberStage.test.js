import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import EmberStage from "./EmberStage.svelte";

async function renderStage(props) {
  const { html } = await render(EmberStage, { props });
  return html.replace(/\s+/g, " ");
}

describe("EmberStage state words", () => {
  it.each([
    ["failed", "Offline"],
    ["evicted", "Offline"],
    ["destroying", "Offline"],
    ["destroyed", "Offline"],
    ["preempted", "Rehoming"],
  ])("renders %s as %s", async (vmState, expected) => {
    expect(await renderStage({ vmState })).toContain(expected);
  });

  it("renders an unmapped state as Offline", async () => {
    expect(await renderStage({ vmState: "future_state" })).toContain("Offline");
  });

  it("explains preemption and shows its duration", async () => {
    const html = await renderStage({
      vmState: "serving",
      preempted: { since_ms: 300_000, phase: "restoring" },
    });

    expect(html).toContain(
      "The Spot node hosting this Postgres was preempted. The control plane is restoring the volume from the object store.",
    );
    expect(html).toContain("down for 5m");
  });
});
