// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ConformanceReplay from "./ConformanceReplay.svelte";
import recording from "./conformance-replay.json";

const last = recording.events.at(-1);
const stateChanges = recording.events.filter(
  (e) => e.action !== "recv_status" && e.action !== "checkpoint",
);
let component;
let target;
async function render() {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(ConformanceReplay, { target });
  await tick();
  return target;
}
const duration = (view) => Number(view.querySelector("input[type=range]").max);
async function seek(at) {
  const timeline = target.querySelector("input[type=range]");
  timeline.value = String(at);
  timeline.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}
const cells = (view) =>
  [...view.querySelectorAll(".cell")].map((li) => li.dataset.verdict);
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  target?.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("the recording is the checker's own answer at every record", () => {
  const keys = recording.invariants.map((i) => i.key);
  let previous = null;
  for (const event of recording.events) {
    expect(Object.keys(event.verdicts).sort()).toEqual([...keys].sort());
    for (const key of keys) {
      const [verdict, coverage] = event.verdicts[key];
      expect(["pass", "fail", "vacuous"]).toContain(verdict);
      if (previous) expect(coverage).toBeGreaterThanOrEqual(previous[key][1]);
    }
    previous = event.verdicts;
  }
  expect(last.at).toBeLessThanOrEqual(recording.durationMs);
});

test("the axis is step time: one unit per state change", async () => {
  const view = await render();
  expect(duration(view)).toBe((stateChanges.length + 1) * 1000);
  const marks = [...view.querySelectorAll(".trow:first-child .track i")];
  expect(marks).toHaveLength(stateChanges.length);
  const lefts = marks.map((m) => parseFloat(m.style.left));
  for (let i = 1; i < lefts.length; i++) {
    expect(lefts[i] - lefts[i - 1]).toBeCloseTo(lefts[1] - lefts[0], 5);
  }
  expect(view.querySelectorAll(".trow")).toHaveLength(2);
});

test("only the rules this run exercises are shown, waiting until checked", async () => {
  const view = await render();
  const exercised = recording.invariants.filter(
    (i) => last.verdicts[i.key][1] > 0,
  );
  expect(cells(view)).toEqual(exercised.map(() => "waiting"));
  expect(view.querySelector(".verdict").textContent.trim()).toBe("");
  await seek(duration(view));
  expect(cells(view)).toEqual(exercised.map(() => "pass"));
  expect(view.querySelector(".cell .v").textContent).toBe(
    `${last.verdicts[exercised[0].key][1]} checked`,
  );
  expect(view.querySelector(".verdict strong").textContent).toBe(
    recording.suiteVerdict,
  );
});

test("VM slots on the brick follow the run and scenarios tick off in order", async () => {
  const view = await render();
  const slots = () => [...view.querySelectorAll(".slot")];
  const done = () =>
    [...view.querySelectorAll(".sc")].filter((g) =>
      g.classList.contains("done"),
    );
  expect(slots().every((s) => !s.hasAttribute("data-state"))).toBe(true);
  // Just after the second state change, the first VM has a task running.
  await seek(2100);
  expect(slots().find((s) => s.dataset.state === "running")).toBeDefined();
  expect(done()).toHaveLength(0);
  await seek(duration(view));
  expect(slots().filter((s) => s.dataset.state === "destroyed")).toHaveLength(
    2,
  );
  expect(done()).toHaveLength(recording.scenarios.length);
  expect(view.querySelector(".big").textContent).toBe(
    `${recording.events.length} records`,
  );
  expect(view.querySelectorAll(".dots circle")).toHaveLength(0);
});

test("records ride their edges for a fixed flight, and play never fetches", async () => {
  vi.useFakeTimers();
  const fetch = vi.spyOn(globalThis, "fetch");
  const view = await render();
  await seek(1300);
  expect(view.querySelectorAll(".dots circle").length).toBeGreaterThanOrEqual(
    2,
  );
  await seek(1900);
  expect(view.querySelectorAll(".dots circle")).toHaveLength(0);
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(1);
  vi.advanceTimersByTime(1000);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBeGreaterThan(
    1900,
  );
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(0);
  expect(fetch).not.toHaveBeenCalled();
});
