// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Harness from "./ExecutionHarness.svelte";
import { createExecutionFixture } from "./execution-fixtures.js";
import { executionLocation } from "./execution-stores.js";

let component;
let target;
let fixture;
const settle = async () => {
  for (let n = 0; n < 12; n++) await tick();
};

function expectPrivateNavigation() {
  expect(
    target.querySelector(
      '.site-index, a[href="/engineering"], a[href="/docs"], a[href="/cv"], a[href^="/app/"], a[href="/ember/firecracker"]',
    ),
  ).toBeNull();
  expect(target.querySelector('.trail-row a[href="/"]')).not.toBeNull();
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-03T12:00:00Z"));
  fixture = createExecutionFixture();
  vi.stubGlobal("fetch", fixture.fetch);
  vi.stubGlobal("EventSource", fixture.EventSource);
  localStorage.clear();
  target = document.createElement("div");
  document.body.append(target);
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  target.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("execution mounts the real layouts, polls a running transcript, streams and tears down fallback timers", async () => {
  executionLocation("execution", "?session=901&keep=synthetic");
  component = mount(Harness, { target });
  await settle();
  expect(target.querySelector("main.td.sheet")).not.toBeNull();
  expect(target.querySelector(".md-nav, .maintenance-banner")).toBeNull();
  expectPrivateNavigation();
  expect(target.querySelector(".inbox-group-count").textContent).toBe("1");
  expect(target.querySelector(".group-count")).toBeNull();
  expect(target.textContent).toContain("Synthetic transcript update 0");
  expect(fixture.stats.active).toBe(1);
  const baselineTimers = vi.getTimerCount();
  fixture.advance();
  await vi.advanceTimersByTimeAsync(2100);
  await settle();
  expect(target.textContent).toContain("Synthetic transcript update 1");
  expect(fixture.stats.calls["/factory/execution/session/901"]).toBeGreaterThan(
    1,
  );
  expect(target.querySelector(".guest-state").textContent).toContain("0");
  fixture.disconnect();
  await settle();
  const firstFallback = vi.getTimerCount();
  fixture.disconnect();
  await settle();
  expect(vi.getTimerCount()).toBe(firstFallback);
  fixture.reconnect();
  await settle();
  expect(vi.getTimerCount()).toBe(baselineTimers);
  const polls = fixture.stats.calls["/factory/execution/vms"];
  await vi.advanceTimersByTimeAsync(2100);
  expect(fixture.stats.calls["/factory/execution/vms"]).toBe(polls);
  expect(fixture.stats.created).toBe(1);
  expect(fixture.stats.maxActive).toBe(1);
  await unmount(component);
  component = null;
  expect(fixture.stats.active).toBe(0);
  expect(fixture.stats.closed).toBe(1);
  expect(vi.getTimerCount()).toBe(0);
  const calls = JSON.stringify(fixture.stats.calls);
  await vi.advanceTimersByTimeAsync(16000);
  expect(JSON.stringify(fixture.stats.calls)).toBe(calls);
  expect(fixture.stats.unexpected).toEqual([]);
});

test("execution renders a selected run and a walkthrough without replacing production route logic", async () => {
  executionLocation("execution", "?run=fixture-running");
  component = mount(Harness, { target });
  await settle();
  expect(target.querySelector(".runview").textContent).toContain(
    "Synthetic running task",
  );
  executionLocation("execution", "?session=901");
  await settle();
  const disclosure = target.querySelector(".turns details.walk");
  expect(disclosure).not.toBeNull();
  disclosure.open = true;
  disclosure.dispatchEvent(new Event("toggle"));
  await settle();
  const points = [...disclosure.querySelectorAll(".point")];
  expect(points).toHaveLength(2);
  expect(points[0].getAttribute("aria-current")).toBe("true");
  expect(points[0].classList.contains("on")).toBe(true);
  points[1].click();
  await settle();
  expect(points[1].getAttribute("aria-current")).toBe("true");
  expect(points[1].classList.contains("on")).toBe(true);
  expect(points[0].classList.contains("on")).toBe(false);
  expect(disclosure.querySelector(".detail").textContent).toContain(
    "selection.js",
  );
  const walkthrough = [...target.querySelectorAll("button")].find(
    (node) => node.textContent.trim() === "Walkthrough",
  );
  expect(walkthrough).toBeTruthy();
  walkthrough.click();
  await settle();
  expect(target.textContent).toContain("synthetic.js");
  expect(fixture.stats.created).toBe(1);
});

test("drain mounts an active item, exposes subsequent polling and releases both clocks", async () => {
  executionLocation("drain");
  component = mount(Harness, { target, props: { view: "drain" } });
  await settle();
  expect(target.querySelector("main.td.sheet")).not.toBeNull();
  expect(target.querySelector(".md-nav, .maintenance-banner")).toBeNull();
  expectPrivateNavigation();
  expect(target.textContent).toContain("Synthetic checkpoint 0");
  expect(target.querySelector(".job-toggle")).not.toBeNull();
  target.querySelector(".job-toggle").click();
  await settle();
  expect(target.querySelector(".job-detail").textContent).toContain(
    "Synthetic drain item",
  );
  fixture.advance();
  await vi.advanceTimersByTimeAsync(5000);
  await settle();
  expect(target.textContent).toContain("Synthetic checkpoint 1");
  expect(fixture.stats.calls["/factory/execution/drain/console"]).toBe(2);
  expect(fixture.stats.active).toBe(0);
  await unmount(component);
  component = null;
  expect(vi.getTimerCount()).toBe(0);
  const calls = JSON.stringify(fixture.stats.calls);
  await vi.advanceTimersByTimeAsync(15000);
  expect(JSON.stringify(fixture.stats.calls)).toBe(calls);
  expect(fixture.stats.unexpected).toEqual([]);
});
