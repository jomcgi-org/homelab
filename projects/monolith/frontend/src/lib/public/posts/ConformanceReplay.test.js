// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ConformanceReplay from "./ConformanceReplay.svelte";
import recording from "./conformance-replay.json";

const last = recording.events.length - 1;
let component;
let target;
async function render() {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(ConformanceReplay, { target });
  await tick();
  return target;
}
async function seek(index) {
  const timeline = target.querySelector("input[type=range]");
  timeline.value = String(index);
  timeline.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}
const verdicts = (view) =>
  [...view.querySelectorAll(".invariants li button")].map(
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
  // Every event carries a verdict for every invariant, and a verdict never
  // steps backwards from pass to vacuous: coverage only grows in a window.
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
  expect(recording.events[0].verdicts.no_double_assign).toEqual(["vacuous", 0]);
  expect(recording.events[last].verdicts.no_double_assign[0]).toBe("pass");
});

test("every invariant opens vacuous and the suite strip appears only at the end", async () => {
  const view = await render();
  expect(verdicts(view)).toEqual(Array(9).fill("vacuous"));
  expect(view.querySelector(".suite")).toBeNull();
  await seek(last);
  const final = recording.events[last].verdicts;
  expect(verdicts(view)).toEqual(
    recording.invariants.map((i) => final[i.key][0]),
  );
  expect(verdicts(view).filter((v) => v === "pass")).toHaveLength(6);
  expect(verdicts(view).filter((v) => v === "vacuous")).toHaveLength(3);
  expect(view.querySelectorAll(".suite li")).toHaveLength(5);
  expect(view.querySelector(".suite strong").textContent).toBe(
    recording.suiteVerdict,
  );
});

test("the trace log and brick follow the scrub position", async () => {
  const view = await render();
  expect(view.querySelectorAll(".trace ol li")).toHaveLength(1);
  const firstDispatch = recording.events.findIndex(
    (e) => e.action === "dispatch_miss",
  );
  await seek(firstDispatch);
  expect(view.querySelectorAll(".trace ol li")).toHaveLength(firstDispatch + 1);
  expect(
    view.querySelector('.trace li[aria-current="step"] .action').textContent,
  ).toBe("dispatch_miss");
  const running = view.querySelector('.vms li[data-state="running"]');
  expect(running.querySelector(".id").textContent).toBe(
    recording.events[firstDispatch].vars.vm,
  );
  await seek(last);
  expect(view.querySelector('.vms li[data-state="destroyed"]')).not.toBeNull();
});

test("play steps one record at a time, pauses cleanly and never fetches", async () => {
  vi.useFakeTimers();
  const fetch = vi.spyOn(globalThis, "fetch");
  const view = await render();
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(1);
  vi.advanceTimersByTime(320 * 3);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(3);
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(0);
  vi.advanceTimersByTime(3200);
  await tick();
  expect(Number(view.querySelector("input[type=range]").value)).toBe(3);
  expect(fetch).not.toHaveBeenCalled();
});

test("selecting an invariant shows its meaning, and the checker's detail once complete", async () => {
  const view = await render();
  const first = view.querySelector(".invariants li button");
  first.click();
  await tick();
  const caption = view.querySelector(".invariants .caption");
  expect(caption.textContent).toContain(recording.invariants[0].meaning);
  expect(caption.textContent).not.toContain("The checker said");
  await seek(last);
  expect(view.querySelector(".invariants .caption").textContent).toContain(
    recording.final[recording.invariants[0].key][2],
  );
});
