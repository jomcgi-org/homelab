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
const range = () => target.querySelector("input[type=range]");
const duration = () => Number(range().max);
async function seek(at) {
  range().value = String(at);
  range().dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}
async function key(k) {
  range().dispatchEvent(
    new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }),
  );
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

test("the axis is step time and arrow keys step between state changes", async () => {
  const view = await render();
  expect(duration()).toBe((stateChanges.length + 1) * 1000);
  const marks = [...view.querySelectorAll(".trow:first-child .track i")];
  expect(marks).toHaveLength(stateChanges.length);
  const lefts = marks.map((m) => parseFloat(m.style.left));
  for (let i = 1; i < lefts.length; i++) {
    expect(lefts[i] - lefts[i - 1]).toBeCloseTo(lefts[1] - lefts[0], 5);
  }
  await key("ArrowRight");
  expect(Number(range().value)).toBe(1000);
  await key("ArrowRight");
  expect(Number(range().value)).toBe(2000);
  await key("ArrowLeft");
  expect(Number(range().value)).toBe(1000);
  expect(view.querySelector(".chapter").textContent).toMatch(
    /1\. clones over vsock\s+record \d+ of 70/,
  );
});

test("only the rules this run exercises are shown, and the end state is unmistakable", async () => {
  const view = await render();
  const exercised = recording.invariants.filter(
    (i) => last.verdicts[i.key][1] > 0,
  );
  expect(cells(view)).toEqual(exercised.map(() => "waiting"));
  expect(view.querySelector(".verdict").textContent.trim()).toBe("");
  await seek(duration());
  expect(cells(view)).toEqual(exercised.map(() => "pass"));
  expect(view.querySelector(".verdict strong").textContent).toBe(
    recording.suiteVerdict,
  );
  expect(view.querySelector(".chapter").textContent.trim()).toBe(
    `${recording.events.length} records replayed, all ${exercised.length} rules passed`,
  );
});

test("clicking a rule lights its evidence and explains it; clicking again clears", async () => {
  const view = await render();
  await seek(duration());
  const rule = [...view.querySelectorAll(".cell")].find((c) =>
    c.textContent.includes("No destroy before confirm"),
  );
  rule.click();
  await tick();
  expect(rule.getAttribute("aria-pressed")).toBe("true");
  expect(view.querySelector(".replay").classList.contains("focused")).toBe(
    true,
  );
  const lit = view.querySelectorAll(".trow .track i.lit");
  const destroys = recording.events.filter(
    (e) => e.action === "begin_destroy" || e.action === "confirm_destroy",
  );
  expect(lit).toHaveLength(destroys.length);
  const litSlots = [...view.querySelectorAll(".slot.lit")];
  expect(litSlots).toHaveLength(2);
  expect(view.querySelector(".why").textContent).toContain(
    "after the node confirmed",
  );
  rule.click();
  await tick();
  expect(view.querySelector(".why")).toBeNull();
  expect(view.querySelectorAll(".trow .track i.lit")).toHaveLength(0);
});

test("scenario rows jump to their chapter, VM slots follow the run", async () => {
  const view = await render();
  const rows = [...view.querySelectorAll(".sc")];
  rows[1].dispatchEvent(new MouseEvent("click", { bubbles: true }));
  await tick();
  expect(view.querySelector(".chapter").textContent).toContain(
    "2. sleep and relight",
  );
  expect(rows[0].classList.contains("done")).toBe(true);
  expect(rows[1].classList.contains("on")).toBe(true);
  const slots = () => [...view.querySelectorAll(".slot")];
  // Clone a has finished at the chapter boundary; clone b finishes just after.
  expect(
    slots().filter((s) => s.dataset.state === "finished").length,
  ).toBeGreaterThanOrEqual(1);
  await seek(duration());
  expect(slots().filter((s) => s.dataset.state === "destroyed")).toHaveLength(
    2,
  );
  expect(rows.every((r) => r.classList.contains("done"))).toBe(true);
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
  expect(Number(range().value)).toBeGreaterThan(1900);
  view.querySelector(".controls button").click();
  await tick();
  expect(vi.getTimerCount()).toBe(0);
  expect(fetch).not.toHaveBeenCalled();
});
