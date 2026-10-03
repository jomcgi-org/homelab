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

test("first-token timing stays fixed when seeking, without a prefill rate or chart", async () => {
  const view = await render();
  const timing = view.querySelector(".measurements > div");
  expect(timing.textContent).toContain("First token");
  expect(timing.textContent).toContain((turn.metrics.ttftMs / 1000).toFixed(1));
  const initial = timing.textContent;
  for (const at of [1000, turn.metrics.ttftMs, turn.durationMs, 0]) {
    await seek(at);
    expect(timing.textContent).toBe(initial);
    expect(view.querySelector(".prefill-history")).toBeNull();
    expect(view.querySelector(".prefill-segment")).toBeNull();
  }
  await seek(turn.durationMs);
  expect(view.querySelector(".answer").textContent.trim()).toBe(
    turn.events.map((e) => e.content).join(""),
  );
});

test("playback advances on frames, changes speed and cancels on pause", async () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  let nextFrame;
  vi.spyOn(globalThis, "requestAnimationFrame").mockImplementation(
    (callback) => {
      nextFrame = callback;
      return 1;
    },
  );
  const cancel = vi.spyOn(globalThis, "cancelAnimationFrame");
  const view = await render();
  view.querySelector(".controls button").click();
  await tick();
  nextFrame(0);
  nextFrame(1000);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(1000);
  const speed = view.querySelector(".speed-control");
  speed.click();
  await tick();
  speed.click();
  await tick();
  nextFrame(1000);
  nextFrame(2000);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(5000);
  view.querySelector(".controls button").click();
  await tick();
  expect(cancel).toHaveBeenCalled();
  expect(view.querySelector(".controls button").textContent).toBe("Play");
  expect(fetch).not.toHaveBeenCalled();
});

test("phase controls seek to exact recorded boundaries and replay restarts", async () => {
  const view = await render();
  const buttons = view.querySelectorAll(".phase-navigation button");
  for (const [index, at] of [0, turn.events[0].at, turn.durationMs].entries()) {
    buttons[index].click();
    await tick();
    expect(Number(view.querySelector("input[type=range]").value)).toBe(at);
    expect(buttons[index].getAttribute("aria-pressed")).toBe("true");
  }
  expect(view.querySelector(".controls button").textContent).toBe("Replay");
  view.querySelector(".controls button").click();
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(0);
});

test("current replay uses captured timings and memory without fabricated routing", async () => {
  const view = await render();
  expect(recording.telemetry.routing).toBe(false);
  expect(view.querySelector(".telemetry")).toBeNull();
  const rate = view.querySelectorAll(".measurements dd")[1];
  expect(rate.textContent).toContain(turn.metrics.tokensPerSecond.toFixed(1));
  await seek(turn.durationMs);
  const lastStats = turn.statsSamples.findLast((sample) => !sample.unavailable);
  expect(view.querySelectorAll(".measurements dd")[2].textContent).toContain(
    (lastStats.vramBytes / 1e9).toFixed(1),
  );
  expect(rate.textContent).toContain(turn.metrics.tokensPerSecond.toFixed(1));
});
