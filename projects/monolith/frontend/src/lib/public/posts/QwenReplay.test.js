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
    "Initial placement",
  );
  const prefill = turn.prefillChunks[0];
  expect(prefill).toBeDefined();
  await seek(prefill.at - 1);
  expect(view.querySelector(".measurements dd").textContent).toContain("--");
  await seek(prefill.at);
  expect(view.querySelector(".measurements").textContent).toContain(
    prefill.tokensPerSecond.toFixed(1),
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
  expect(view.querySelector(".prefill-history line")).not.toBeNull();
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

test("prefill and boundary routing samples are excluded without fabricating sparkline points", async () => {
  const view = await render();
  expect(view.querySelectorAll(".prefill-history circle")).toHaveLength(0);
  await seek(turn.events[0].at);
  expect(view.querySelectorAll(".routing-history rect")).toHaveLength(0);
  expect(view.querySelector(".routing-heading").textContent).toContain(
    "Initial placement",
  );
  const expected = turn.prefillChunks;
  expect(view.querySelectorAll(".prefill-history circle")).toHaveLength(
    expected.filter((item) => item.at <= turn.events[0].at).length,
  );
  await seek(turn.durationMs);
  expect(view.querySelectorAll(".prefill-history circle")).toHaveLength(
    expected.length,
  );
  expect(view.textContent).not.toContain("No routing samples");
});

test("prefill displays only measured chunk averages, with token and throughput units", async () => {
  const view = await render();
  const a = turn.prefillChunks[0];
  await seek(a.at / 2);
  expect(view.querySelector(".prefill-segment")).toBeNull();
  await seek(a.at);
  const segment = view.querySelector(".prefill-segment");
  expect(Number(segment.getAttribute("x2"))).toBeCloseTo(
    (300 * a.tokens) / turn.metrics.prefillTokens,
  );
  expect(segment.getAttribute("y1")).toBe(segment.getAttribute("y2"));
  const y = segment.getAttribute("y1");
  await seek(turn.durationMs);
  expect(view.querySelector(".prefill-segment").getAttribute("y1")).toBe(y);
  expect(view.querySelector(".prefill-history header").textContent).toContain(
    "tok/s",
  );
  expect(
    view.querySelector(".prefill-history .history-labels").textContent,
  ).toContain("tokens");
  expect(view.querySelector(".prefill-history .caption").textContent).toContain(
    "First token",
  );
  for (const chunk of turn.prefillChunks) {
    expect(chunk.tokensPerSecond).toBeCloseTo(
      (chunk.tokens * 1000) / chunk.elapsedMs,
    );
  }
  expect(turn.statsSamples.every((item) => item.prefillTps === undefined)).toBe(
    true,
  );
});
