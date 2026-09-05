// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import QwenReplay from "./QwenReplay.svelte";
import recording from "./qwen-replay.json";

let component;
let target;
async function render() {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(QwenReplay, { target });
  await tick();
  return target;
}
afterEach(async () => {
  if (component) await unmount(component);
  target?.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("backward seeking clears future output and telemetry without changing measured performance", async () => {
  const view = await render();
  const metrics = view.querySelector(".measurements").textContent;
  const answer = view.querySelector(".answer");
  expect(answer.textContent).toContain(
    recording.turns[0].events.at(-1).content,
  );
  const timeline = view.querySelector("input[type=range]");
  timeline.value = "0";
  timeline.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
  expect(answer.textContent).toContain("Waiting for the first token");
  expect(view.querySelector(".telemetry").textContent).toContain(
    "No telemetry sample available",
  );
  expect(view.querySelector(".measurements").textContent).toBe(metrics);
  timeline.value = String(recording.turns[0].durationMs);
  timeline.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
  expect(answer.textContent.trim()).toBe(
    recording.turns[0].events.map((e) => e.content).join(""),
  );
});

test("switching turns stops playback, and replay never calls the live service", async () => {
  vi.useFakeTimers();
  const fetch = vi.spyOn(globalThis, "fetch");
  const view = await render();
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(1);
  view.querySelectorAll(".turns button")[1].click();
  await tick();
  expect(vi.getTimerCount()).toBe(0);
  expect(view.querySelector("input[type=range]").value).toBe(
    String(recording.turns[1].durationMs),
  );
  expect(view.querySelector(".answer").textContent.trim()).toBe(
    recording.turns[1].events.map((e) => e.content).join(""),
  );
  expect(fetch).not.toHaveBeenCalled();
});

test("tier keys highlight matching routing and history above the transcript", async () => {
  const view = await render();
  const telemetry = view.querySelector(".telemetry");
  expect(
    telemetry.compareDocumentPosition(view.querySelector(".answer")) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  const hot = view.querySelector("button.tier.hot");
  hot.click();
  await tick();
  expect(hot.getAttribute("aria-pressed")).toBe("true");
  expect(
    view.querySelector(".activity-bar .warm").classList.contains("dimmed"),
  ).toBe(true);
  expect(
    view
      .querySelector(".routing-history rect.hot")
      .classList.contains("dimmed"),
  ).toBe(false);
  expect(view.querySelector(".tier-description").textContent).toContain(
    "GPU slots",
  );
  hot.click();
  await tick();
  expect(view.querySelectorAll(".dimmed").length).toBe(0);
});
