// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ConformanceReplay from "./ConformanceReplay.svelte";
import recording from "./conformance-replay.json";

const last = recording.events.at(-1);
let component;
let target;
async function render() {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(ConformanceReplay, { target });
  await tick();
  return target;
}
async function seek(at) {
  const timeline = target.querySelector("input[type=range]");
  timeline.value = String(at);
  timeline.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}
const verdicts = (view) =>
  [...view.querySelectorAll(".invariants button")].map(
    (b) => b.dataset.verdict,
  );
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  target?.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("the recording is the checker's own answer at every record", () => {
  // Every event carries a verdict for every invariant, and coverage only
  // grows inside a window.
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
  expect(recording.events[0].verdicts.no_double_assign).toEqual(["vacuous", 0]);
  expect(last.verdicts.no_double_assign[0]).toBe("pass");
});

test("every invariant opens vacuous and the suite strip appears only at the end", async () => {
  const view = await render();
  expect(verdicts(view)).toEqual(Array(9).fill("vacuous"));
  expect(view.querySelector(".suite")).toBeNull();
  expect(view.querySelectorAll(".ticks i")).toHaveLength(
    recording.events.length,
  );
  await seek(recording.durationMs);
  expect(verdicts(view)).toEqual(
    recording.invariants.map((i) => last.verdicts[i.key][0]),
  );
  expect(verdicts(view).filter((v) => v === "pass")).toHaveLength(6);
  expect(verdicts(view).filter((v) => v === "vacuous")).toHaveLength(3);
  expect(view.querySelector(".suite strong").textContent).toBe(
    recording.suiteVerdict,
  );
  expect(view.querySelector(".suite").textContent).toContain("Kargo promotes");
});

test("VM bars grow under the playhead and the current record follows the scrub", async () => {
  const view = await render();
  expect(view.querySelectorAll(".lane .bar i")).toHaveLength(0);
  const dispatch = recording.events.find((e) => e.action === "dispatch_miss");
  await seek(dispatch.at);
  expect(view.querySelector(".now .what").textContent).toBe(
    "a task is dispatched to it",
  );
  const running = view.querySelector('.lane .bar i[data-state="running"]');
  expect(running).not.toBeNull();
  const lane = running.closest(".lane");
  expect(lane.querySelector(".id").textContent).toBe(
    recording.roles[dispatch.vars.vm],
  );
  // The bar for a VM primed later has not appeared yet.
  const later = recording.events.find(
    (e) => e.action === "prime" && e.at > dispatch.at,
  );
  const laterLane = [...view.querySelectorAll(".lane")].find(
    (l) =>
      l.querySelector(".id").textContent === recording.roles[later.vars.vm],
  );
  expect(laterLane.querySelectorAll(".bar i")).toHaveLength(0);
  await seek(recording.durationMs);
  expect(laterLane.querySelectorAll(".bar i").length).toBeGreaterThan(0);
  expect(
    view.querySelector('.lane .bar i[data-state="destroyed"]'),
  ).not.toBeNull();
});

test("play advances at 8x, pauses cleanly and never fetches", async () => {
  vi.useFakeTimers();
  const fetch = vi.spyOn(globalThis, "fetch");
  const view = await render();
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(1);
  vi.advanceTimersByTime(1000);
  await tick();
  const after = Number(view.querySelector("input[type=range]").value);
  expect(after).toBeGreaterThan(7000);
  expect(after).toBeLessThanOrEqual(8200);
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(0);
  vi.advanceTimersByTime(1000);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(after);
  expect(fetch).not.toHaveBeenCalled();
});

test("selecting an invariant shows its meaning, and the checker's detail once complete", async () => {
  const view = await render();
  const first = view.querySelector(".invariants button");
  first.click();
  await tick();
  const note = view.querySelector(".note");
  expect(note.textContent).toContain(recording.invariants[0].meaning);
  expect(note.textContent).not.toContain("The checker said");
  await seek(recording.durationMs);
  expect(view.querySelector(".note").textContent).toContain(
    recording.final[recording.invariants[0].key][2],
  );
});
