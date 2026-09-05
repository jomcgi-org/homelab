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

test("initial placement stays stable until polling and controls precede the instrument", async () => {
  const view = await render();
  const first = turn.samples.find(
    (item, index) =>
      index > 0 && turn.samples[index - 1].at >= turn.events[0].at,
  );
  const bar = view.querySelector(".activity-bar");
  const starting = bar.innerHTML;
  const capacity = turn.samples.find((item) => item.tiers).tiers.hotBytes;
  expect(view.querySelector(".capacity").textContent).toContain(
    (capacity / 1e9).toFixed(1) + " GB",
  );
  expect(
    view
      .querySelector(".controls")
      .compareDocumentPosition(view.querySelector(".instrument")) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  await seek(first.at - 1);
  expect(bar.innerHTML).toBe(starting);
  expect(view.querySelector(".routing-history rect.hot")).toBeNull();
  expect(view.querySelector(".prefill-history")).toBeNull();
  await seek(first.at);
  expect(view.querySelector(".routing-heading").textContent).toContain(
    "activations",
  );
  expect(bar.innerHTML).not.toBe(starting);
  const firstHot = view.querySelector(".routing-history rect.hot");
  expect(Number(firstHot.getAttribute("x"))).toBeCloseTo(0);
  await seek(0);
  expect(bar.innerHTML).toBe(starting);
});

test("the final routing split reaches the end without displaying idle samples as gaps", async () => {
  const view = await render();
  await seek(turn.durationMs);
  expect(view.querySelector(".sample-status")).toBeNull();
  const hot = [...view.querySelectorAll(".routing-history rect.hot")]
    .filter((rect) => Number(rect.getAttribute("width")) > 0)
    .at(-1);
  expect(
    Number(hot.getAttribute("x")) + Number(hot.getAttribute("width")),
  ).toBeCloseTo(700);
  expect(view.querySelector(".tier.hot strong").textContent).toContain(
    "of routes",
  );
});

test("prefill and boundary intervals stay out of the decode routing chart", async () => {
  const view = await render();
  await seek(turn.events[0].at);
  expect(view.querySelectorAll(".routing-history rect")).toHaveLength(0);
  expect(view.querySelector(".routing-heading").textContent).toContain(
    "Initial placement",
  );
  await seek(turn.durationMs);
  expect(view.querySelectorAll(".routing-history rect").length).toBeGreaterThan(
    0,
  );
});
