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

test("playback advances in real time and cancels on pause", async () => {
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
  nextFrame(500);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(500);
  nextFrame(1000);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(1000);
  expect(view.querySelector(".speed-control")).toBeNull();
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
  expect(view.querySelectorAll(".measurements dd")[3].textContent).toContain(
    (lastStats.vramBytes / 1e9).toFixed(1),
  );
  expect(rate.textContent).toContain(turn.metrics.tokensPerSecond.toFixed(1));
});

test("shows effective uncached prefill throughput without speed or arrival-count clutter", async () => {
  const view = await render();
  const rate = Math.round(
    ((turn.usage.prompt_tokens - turn.usage.cached_tokens) * 1000) /
      turn.metrics.ttftMs,
  );
  expect(view.querySelectorAll(".measurements dd")[2].textContent).toContain(
    rate.toLocaleString("en-US"),
  );
  expect(view.querySelector(".speed-control")).toBeNull();
  expect(view.querySelector(".arrival-count")).toBeNull();
});

test("the input scan follows seeking while thinking stays explicitly disabled", async () => {
  const view = await render();
  expect(recording.thinking).toBe(false);
  expect(turn.events.every((event) => !event.reasoning)).toBe(true);
  const initial = view.querySelector(".scan-window").textContent;
  await seek(turn.metrics.ttftMs / 2);
  expect(view.querySelector(".scan-window").textContent).not.toBe(initial);
  expect(view.querySelector(".scan-heading").textContent).toContain(
    "Thinking off",
  );
  await seek(0);
  expect(view.querySelector(".scan-window").textContent).toBe(initial);
});
