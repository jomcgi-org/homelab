// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import { load } from "./+page.server.js";
import { NOW, payloads } from "../../../../../factory-preview/fixtures.js";

let instance;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(NOW));
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
const task = (issue_number, state) => ({
  issue_number,
  state,
  title: `Synthetic task ${issue_number}`,
  admitted_at: NOW,
  finished_at: NOW,
  nodes: [],
});
async function render(active, snapshotted_at = NOW, unavailable = false) {
  const endpoints = payloads("live");
  const data = await load({
    setHeaders() {},
    fetch: async (url) => new Response(JSON.stringify(endpoints[url])),
  });
  data.board = {
    ...data.board,
    snapshotted_at,
    active,
    queued: [task(7, "queued")],
    recent: ["landed", "cancelled", "escalated"].map((state, i) =>
      task(10 + i, state),
    ),
  };
  data.unavailable.board = unavailable;
  const target = document.createElement("div");
  document.body.append(target);
  instance = mount(Page, { target, props: { data } });
  await tick();
  return { target, board: data.board };
}
it.each([
  ["mixed terminal history", [], NOW],
  ["paused and uncertain", [task(1, "paused"), task(2, "uncertain")], NOW],
  ["terminal escalation", [task(1, "escalated")], NOW],
  ["legacy in flight", [task(1, "in flight")], NOW],
  ["stale execution", [task(1, "executing")], "2026-10-03T11:54:00Z"],
])("shows honest idle state for %s", async (_, active, snapshot) => {
  const { target } = await render(active, snapshot);
  expect(target.querySelector(".lane-list")).toBeNull();
  expect(target.textContent).toContain("No confirmed execution right now.");
  expect(
    target.querySelector('.goals a[href="/slop/factory/activity"]').textContent,
  ).toContain("all activity");
});
it("shows only confirmed execution with task age", async () => {
  const { target } = await render([
    task(1, "executing"),
    task(2, "paused"),
    task(3, "uncertain"),
    task(4, "landing"),
  ]);
  const rows = target.querySelectorAll(".lane-list li");
  expect(rows).toHaveLength(1);
  expect(rows[0].textContent).toContain("#1");
  expect(rows[0].textContent).toContain("task age");
});
it("expires execution while refresh is unavailable", async () => {
  const { target } = await render([task(1, "executing")]);
  expect(target.querySelectorAll(".lane-list li")).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(6 * 60_000);
  await tick();
  expect(target.querySelector(".lane-list")).toBeNull();
  expect(target.textContent).toContain("No confirmed execution right now.");
});
it("updates current work when the existing board refresh reports a pause", async () => {
  const { target, board } = await render([task(1, "executing")]);
  fetch.mockResolvedValue(
    new Response(JSON.stringify({ ...board, active: [task(1, "paused")] })),
  );
  await vi.advanceTimersByTimeAsync(60_000);
  await tick();
  expect(target.querySelector(".lane-list")).toBeNull();
});

it("recovers an initially unavailable board on successful refresh", async () => {
  const { target, board } = await render([], NOW, true);
  expect(target.querySelector(".goals").textContent).toContain(
    "Unavailable right now.",
  );
  fetch.mockResolvedValue(
    new Response(JSON.stringify({ ...board, active: [task(1, "executing")] })),
  );
  await vi.advanceTimersByTimeAsync(60_000);
  await tick();
  expect(target.querySelectorAll(".lane-list li")).toHaveLength(1);
  expect(target.querySelector(".goals").textContent).not.toContain(
    "Unavailable right now.",
  );
});
