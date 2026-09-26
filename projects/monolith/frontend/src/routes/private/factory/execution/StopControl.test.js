// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import StopControl from "./StopControl.svelte";

const mounted = [];
const identity = { turn_seq: 4, dispatch_id: "dispatch-4" };

async function render(props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(StopControl, {
    target,
    props: { enabled: false, identity, onStop: vi.fn(), ...props },
  });
  mounted.push({ component, target });
  await tick();
  return target;
}

afterEach(async () => {
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("Stop control", () => {
  test("is absent while the rollout flag is false", async () => {
    const target = await render();
    expect(target.querySelector("button")).toBe(null);
  });

  test("sends the exact identity and disables duplicate requested clicks", async () => {
    const onStop = vi.fn();
    const target = await render({ enabled: true, onStop });
    target.querySelector("button").click();
    await tick();
    expect(onStop).toHaveBeenCalledWith(identity);

    const pending = await render({
      enabled: true,
      status: { ...identity, outcome: "requested" },
    });
    expect(pending.querySelector("button").disabled).toBe(true);
    expect(pending.textContent).toContain("waiting for the turn result");
  });

  test.each([
    ["pending", "Stop request pending"],
    ["confirmed", "Turn stopped"],
    ["completed", "completed before Stop"],
    ["failed", "Stop failed"],
    ["unknown", "outcome unknown"],
  ])("presents %s truthfully", async (outcome, text) => {
    const target = await render({
      enabled: true,
      status: { ...identity, outcome },
    });
    expect(target.textContent).toContain(text);
  });
});
