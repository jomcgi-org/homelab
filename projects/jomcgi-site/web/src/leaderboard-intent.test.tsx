import { act, fireEvent, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { html } from "./leaderboard-static";
import { leaderboard } from "./leaderboard-data";
import { onLeaderboardIntent } from "./leaderboard-intent";

const hosts: { host: HTMLElement; dispose: () => void }[] = [];
function deferredReport() {
  const host = document.createElement("div");
  host.innerHTML = html;
  document.body.append(host);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const load = vi.fn(async () => {
    await gate;
    return import("./leaderboard-interactive");
  });
  const dispose = onLeaderboardIntent(host, load);
  hosts.push({ host, dispose });
  return { host, load, release, dispose, view: within(host) };
}

afterEach(async () => {
  await act(async () => {
    for (const { host, dispose } of hosts.splice(0)) {
      dispose();
      host.remove();
    }
  });
  window.history.replaceState(null, "", "/");
});

it("keeps the static charts readable with no import, then replays the first click after a slow download", async () => {
  const { host, view, load, release } = deferredReport();
  expect(load).not.toHaveBeenCalled();
  expect(view.getByRole("table", { name: "Ranked models" })).toBeTruthy();
  expect(host.querySelectorAll(".bars svg rect").length).toBeGreaterThan(0);
  fireEvent.click(view.getByRole("button", { name: "Clear all" }));
  expect(load).toHaveBeenCalledTimes(1);
  expect(view.getByRole("table", { name: "Ranked models" })).toBeTruthy();
  await act(async () => {
    release();
    await load.mock.results[0]!.value;
  });
  expect(view.queryByRole("table", { name: "Ranked models" })).toBeNull();
  expect(new URL(window.location.href).searchParams.get("m")).toBeNull();
  fireEvent.click(view.getByRole("button", { name: "All" }));
  expect(load).toHaveBeenCalledTimes(1);
  expect(host.querySelectorAll(".ranked tbody tr")).toHaveLength(
    leaderboard.models.length,
  );
});

it("preserves typed search and Enter during the download without trapping Tab", async () => {
  const { view, load, release, host } = deferredReport();
  const search = view.getByRole("searchbox", { name: "Search models" });
  fireEvent.focusIn(search);
  expect(load).toHaveBeenCalledTimes(1);
  expect(fireEvent.keyDown(search, { key: "Tab" })).toBe(true);
  fireEvent.input(search, { target: { value: "qwen" } });
  fireEvent.keyDown(search, { key: "Enter" });
  await act(async () => {
    release();
    await load.mock.results[0]!.value;
  });
  expect((search as HTMLInputElement).value).toBe("qwen");
  expect(host.querySelector("details")?.open).toBe(true);
  const ids = [...host.querySelectorAll(".ranked tbody tr")].map((row) =>
    row.getAttribute("data-model"),
  );
  for (const model of leaderboard.models.filter((model) =>
    model.id.toLowerCase().includes("qwen"),
  )) {
    expect(ids).toContain(model.id);
  }
});

it("warms on pointer intent and cancels hydration if the report is removed", async () => {
  const { host, load, release, dispose, view } = deferredReport();
  fireEvent.pointerOver(view.getByRole("button", { name: "All" }));
  expect(load).toHaveBeenCalledTimes(1);
  dispose();
  const before = host.innerHTML;
  await act(async () => {
    release();
    await load.mock.results[0]!.value;
  });
  fireEvent.click(view.getByRole("button", { name: "Clear all" }));
  expect(host.innerHTML).toBe(before);
});

it("treats a linked selection as intent without waiting for a pointer", async () => {
  const model = leaderboard.models[0]!;
  window.history.replaceState(
    null,
    "",
    "/llm-leaderboard?m=" + encodeURIComponent(model.id),
  );
  const { host, load, release } = deferredReport();
  expect(load).toHaveBeenCalledTimes(1);
  await act(async () => {
    release();
    await load.mock.results[0]!.value;
  });
  expect(
    [...host.querySelectorAll(".ranked tbody tr")].map((row) =>
      row.getAttribute("data-model"),
    ),
  ).toEqual([model.id]);
});

it("does not delay native links behind the interactive chunk", () => {
  const { view, load } = deferredReport();
  expect(
    fireEvent.click(view.getByRole("link", { name: "projects/model-bench" })),
  ).toBe(true);
  expect(load).not.toHaveBeenCalled();
});
