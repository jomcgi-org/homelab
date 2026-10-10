// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import QwenReplay from "./QwenReplay.svelte";
import { incidentGraph } from "./incident-graph.js";
import { decodeSteps } from "./draft-steps.js";
import recording from "./qwen-replay.json";

const turn = recording.turns[0];
let component;
let target;
async function render(props = {}) {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(QwenReplay, { target, props });
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
  vi.unstubAllGlobals();
});

test("the landing keeps playback first and captured output secondary", async () => {
  const view = await render({ landing: true });
  expect(view.querySelectorAll(".controls")).toHaveLength(1);
  expect(
    view
      .querySelector(".controls")
      .compareDocumentPosition(view.querySelector(".demo-body")) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  await seek(turn.durationMs);
  const toggle = view.querySelector(".model-output button");
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  toggle.click();
  await tick();
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(view.querySelector(".model-output code").textContent).toBe(
    turn.events.map((event) => event.content).join(""),
  );
});

test("reduced motion leaves the landing paused until Play is requested", async () => {
  const observe = vi.fn();
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe = observe;
      disconnect() {}
    },
  );
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  const view = await render({ landing: true });
  expect(observe).not.toHaveBeenCalled();
  expect(view.querySelector(".controls button").textContent).toBe("Play");
  view.querySelector(".controls button").click();
  await tick();
  expect(view.querySelector(".controls button").textContent).toBe("Pause");
});

test("measurements fill in as the replay reaches them, in request order", async () => {
  const view = await render();
  const slots = () =>
    [...view.querySelectorAll(".measurements > div")].map((d) => d.textContent);
  const labels = [...view.querySelectorAll(".measurements dt")].map((d) =>
    d.textContent.trim(),
  );
  expect(labels).toEqual(["Prefill", "First token", "Decode"]);
  const tokens = (
    turn.usage.prompt_tokens - turn.usage.cached_tokens
  ).toLocaleString("en-US");
  await seek(1000);
  let [prefill, first, decode] = slots();
  expect(prefill).toContain(`reading ${tokens} tokens`);
  expect(first).toContain("--");
  expect(decode).toContain("--");
  await seek(turn.metrics.ttftMs);
  [prefill, first, decode] = slots();
  expect(first).toContain((turn.metrics.ttftMs / 1000).toFixed(1));
  expect(decode).toContain("tok/s now");
  await seek(turn.durationMs);
  [prefill, first, decode] = slots();
  expect(decode).toContain(turn.metrics.tokensPerSecond.toFixed(1));
  expect(view.querySelector(".prefill-history")).toBeNull();
  const output = turn.events.map((e) => e.content).join("");
  expect(view.querySelector(".incident-graph pre").textContent).toBe(output);
  expect(view.querySelectorAll(".graph-node").length).toBe(
    incidentGraph(output, true).nodes.length,
  );
  expect(view.querySelector(".answer > p")).toBeNull();
});

test("the output panel opens while the model writes and closes when it finishes", async () => {
  const view = await render({ landing: true });
  await seek(turn.events[0].at + 1);
  const toggle = () => view.querySelector(".model-output button");
  expect(toggle().getAttribute("aria-expanded")).toBe("true");
  await seek(turn.durationMs);
  expect(toggle().getAttribute("aria-expanded")).toBe("false");
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

test("the scrubber seeks across request phases and replay restarts", async () => {
  const view = await render();
  for (const [at, phase] of [
    [0, "Prefill"],
    [turn.events[0].at, "Decode"],
    [turn.durationMs, "Complete"],
  ]) {
    await seek(at);
    expect(Number(view.querySelector("input[type=range]").value)).toBe(at);
    expect(view.querySelector('[role="status"]').textContent).toBe(phase);
  }
  expect(view.querySelector(".controls button").textContent).toBe("Replay");
  view.querySelector(".controls button").click();
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(0);
});

test("current replay uses captured timings without fabricated routing", async () => {
  const view = await render();
  expect(recording.telemetry.routing).toBe(false);
  expect(view.querySelector(".telemetry")).toBeNull();
  await seek(turn.durationMs);
  expect(view.querySelectorAll(".measurements dd")).toHaveLength(3);
  expect(view.querySelectorAll(".measurements dd")[2].textContent).toContain(
    turn.metrics.tokensPerSecond.toFixed(1),
  );
});

test("shows effective uncached prefill throughput without speed or arrival-count clutter", async () => {
  const view = await render();
  const rate = Math.round(
    ((turn.usage.prompt_tokens - turn.usage.cached_tokens) * 1000) /
      turn.metrics.ttftMs,
  );
  await seek(turn.durationMs);
  expect(view.querySelectorAll(".measurements dd")[0].textContent).toContain(
    rate.toLocaleString("en-US"),
  );
  expect(view.querySelector(".speed-control")).toBeNull();
  expect(view.querySelector(".arrival-count")).toBeNull();
});

test("the input scan follows seeking while thinking stays explicitly disabled", async () => {
  const view = await render();
  expect(recording.thinking).toBe(false);
  expect(turn.events.every((event) => !event.reasoning)).toBe(true);
  const initial = view.querySelector(".document-pages").getAttribute("style");
  await seek(turn.metrics.ttftMs / 2);
  expect(view.querySelector(".document-pages").getAttribute("style")).not.toBe(
    initial,
  );
  expect(view.querySelector(".scan-heading").textContent).toContain(
    "Thinking off",
  );
  await seek(0);
  expect(view.querySelector(".document-pages").getAttribute("style")).toBe(
    initial,
  );
});

test("model output is open during decode and contains only arrived text", async () => {
  const view = await render();
  const at = turn.events.find(
    (event) => event.at > turn.events[0].at + 1000,
  ).at;
  await seek(at);
  const pane = view.querySelector(".model-output");
  const toggle = pane.querySelector("button");
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const arrived = turn.events
    .filter((event) => event.at <= at)
    .map((event) => event.content)
    .join("");
  expect(pane.querySelector("code").textContent).toBe(arrived);
  expect(arrived.length).toBeLessThan(
    turn.events.map((event) => event.content).join("").length,
  );
  expect(pane.querySelector(".stream-cursor")).not.toBeNull();
  toggle.click();
  await tick();
  await seek(at + 500);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(pane.querySelector(".disclosure-panel").inert).toBe(true);
  await seek(turn.durationMs);
  expect(pane.querySelector(".stream-cursor")).toBeNull();
});

test("the research answer shows a bar per decode step, mostly kept model drafts", async () => {
  const view = await render();
  const steps = decodeSteps(turn.events);
  await seek(turn.durationMs);
  expect(view.querySelectorAll(".trace rect")).toHaveLength(steps.length);
  const drafts = view.querySelectorAll(".trace rect.draft").length;
  expect(drafts).toBeGreaterThan(
    view.querySelectorAll(".trace rect.lookup").length,
  );
  // The code tint only applies to the coding demo.
  expect(view.querySelector(".trace figcaption").textContent).not.toContain(
    "tinted",
  );
});
