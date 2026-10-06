// @vitest-environment happy-dom
import { afterEach, expect, test } from "vitest";
import { mount, tick, unmount } from "svelte";
import QwenReplay from "./QwenReplay.svelte";
import { decodeSteps } from "./draft-steps.js";
import recording from "./agent-replay.json";

const turn = recording.turns[0];
let component;
let target;
async function render() {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(QwenReplay, {
    target,
    props: { kind: "coding", recording },
  });
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
});

test("the recording's token counts match the server's", () => {
  const tokens = turn.events.reduce((n, e) => n + (e.tokens ?? 1), 0);
  expect(tokens).toBe(turn.usage.completion_tokens);
});

test("prefill scans the crate's files as pages", async () => {
  const view = await render();
  const headings = [...view.querySelectorAll(".page-heading")].map(
    (h) => h.textContent,
  );
  expect(headings).toContain("src/cache.rs");
  expect(headings).toHaveLength(recording.document.pages);
});

test("decode streams the code with copied text tinted and one bar per step", async () => {
  const view = await render();
  const steps = decodeSteps(turn.events);
  await seek(turn.durationMs);
  const code = view.querySelector(".code-output code").textContent;
  expect(code).toBe(turn.events.map((e) => e.content).join(""));
  expect(code).toContain("fn occupancy");
  expect(view.querySelectorAll(".code-output .copied").length).toBeGreaterThan(0);
  expect(view.querySelectorAll(".trace rect")).toHaveLength(steps.length);
  expect(view.querySelectorAll(".trace rect.pending")).toHaveLength(0);
});

test("steps ahead of the playhead stay pending", async () => {
  const view = await render();
  const steps = decodeSteps(turn.events);
  const mid = steps[Math.floor(steps.length / 2)].at;
  await seek(mid);
  const pending = view.querySelectorAll(".trace rect.pending").length;
  expect(pending).toBe(steps.filter((s) => s.at > mid).length);
  expect(view.querySelector(".code-output .rate").textContent).toMatch(/tok\/s now/);
});
