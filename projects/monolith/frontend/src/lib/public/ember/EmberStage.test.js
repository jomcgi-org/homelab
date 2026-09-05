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
  ])("renders %s as %s", async (vmState, expected) => {
    expect(await renderStage({ vmState })).toContain(expected);
  });

  it.each([null, "future_state"])(
    "renders unknown state %s as neutral asleep copy",
    async (vmState) => {
      expect(await renderStage({ vmState })).toContain("Asleep");
      expect(await renderStage({ vmState })).not.toContain("Offline");
    },
  );

  it("keeps a draining serving instance in its normal state", async () => {
    const html = await renderStage({
      vmState: "serving",
      preempted: { since_ms: 0, phase: "confirming" },
      displayPreempted: false,
    });

    expect(html).toContain("Awake");
    expect(html).not.toContain("Rehoming");
    expect(html).not.toContain("shut down at short notice");
  });

  it.each([
    ["confirming", "It is checking whether the machine is really gone."],
    ["restoring", "It is coming back with its data."],
    ["cold", "It is starting fresh."],
  ])("explains the %s preemption phase", async (phase, phaseCopy) => {
    const html = await renderStage({
      vmState: "failed",
      preempted: { since_ms: 300_000, phase },
      displayPreempted: true,
    });

    expect(html).toContain(
      "The machine running this database was shut down at short notice, which is normal on the cheap capacity it runs on. It is coming back automatically with its data intact.",
    );
    expect(html).toContain(phaseCopy);
    expect(html).toContain("Rehoming");
    expect(html).toContain("down for 5m");
  });
});
