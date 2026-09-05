// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import QwenReplay from "./QwenReplay.svelte";
import recording from "./qwen-replay.json";

const turn = recording.turns[0];
let component;
let target;
async function render() {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(QwenReplay, { target });
  await tick();
  return target;
}
async function seek(at) {
  const timeline = target.querySelector("input[type=range]");
  timeline.value = String(at);
  timeline.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  target?.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("seeking restores recorded output and independent prefill statistics", async () => {
  const view = await render();
  await seek(turn.durationMs);
  expect(view.querySelector(".answer").textContent.trim()).toBe(
    turn.events.map((e) => e.content).join(""),
  );
  await seek(0);
  expect(view.querySelector(".answer").textContent).toContain(
    "Waiting for the first token",
  );
  expect(view.querySelector(".telemetry").textContent).toContain(
    "No telemetry sample available",
  );
  const prefill = turn.statsSamples.find(
    (sample) => !sample.unavailable && sample.at < turn.samples[0].at,
  );
  expect(prefill).toBeDefined();
  await seek(prefill.at);
  expect(view.querySelector(".phase").textContent).toContain("Prefill");
  expect(view.querySelector(".measurements").textContent).toContain(
    prefill.prefillTps.toFixed(1),
  );
  expect(view.querySelector(".telemetry").textContent).toContain(
    "No telemetry sample available",
  );
});

test("the single real-time session pauses cleanly and never calls inference", async () => {
  vi.useFakeTimers();
  const fetch = vi.spyOn(globalThis, "fetch");
  const view = await render();
  expect(recording.turns).toHaveLength(1);
  expect(view.querySelector(".turns")).toBeNull();
  expect(view.querySelector("select")).toBeNull();
  expect(view.querySelector(".recording-notes")).toBeNull();
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(1);
  vi.advanceTimersByTime(1000);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(1000);
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(0);
  vi.advanceTimersByTime(1000);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(1000);
  expect(fetch).not.toHaveBeenCalled();
});

test("tier keys highlight routing and history above the transcript", async () => {
  const view = await render();
  await seek(turn.durationMs);
  expect(
    view
      .querySelector(".telemetry")
      .compareDocumentPosition(view.querySelector(".answer")) &
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
  hot.click();
  await tick();
  expect(view.querySelectorAll(".dimmed").length).toBe(0);
});
