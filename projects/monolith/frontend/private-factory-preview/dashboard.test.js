import { afterEach, expect, test, vi } from "vitest";
import { mount, unmount, tick } from "svelte";
import App from "./App.svelte";
import { fixtureData } from "./fixtures.js";
import { launcher } from "../src/lib/private/launcher.js";
import { setRoute } from "./state.js";

let instance;
const settle = async () => {
  for (let i = 0; i < 12; i++) await tick();
};
const response = (value) => ({
  ok: true,
  json: async () => value,
  text: async () => JSON.stringify(value),
});
async function render(
  scenario = "populated",
  data = fixtureData("dashboard", scenario),
) {
  setRoute("dashboard");
  instance = mount(App, {
    target: document.body,
    props: { view: "dashboard", data },
  });
  await settle();
}
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  document.body.innerHTML = "";
  delete document.documentElement.dataset.theme;
  localStorage.clear();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
test("preserves sections, launcher destinations and deduplicated daily tasks", async () => {
  await render();
  for (const label of [
    "Today",
    "Tasks",
    "Shipping",
    "Launcher",
    "Ask the cluster",
  ])
    expect(
      [...document.querySelectorAll("h2")].some(
        (node) => node.textContent === label,
      ),
    ).toBe(true);
  for (const item of launcher) {
    const link = [...document.querySelectorAll(".tile")].find((node) =>
      node.textContent.includes(item.label),
    );
    expect(link.getAttribute("href")).toBe(item.href);
    expect(link.getAttribute("target")).toBe(item.external ? "_blank" : null);
  }
  expect(
    document.querySelectorAll('[aria-label="Toggle Synthetic daily task"]'),
  ).toHaveLength(1);
  expect(document.querySelectorAll(".task-row")).toHaveLength(2);
  expect(document.querySelector(".back-to-dashboard")).toBeNull();
  expect(document.querySelectorAll(".scheme")).toHaveLength(1);
});
test("loads the dashboard typeface outside the bundled fixture font", async () => {
  await render();
  expect(
    document.head.querySelector(
      'link[rel="stylesheet"][href^="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:"]',
    ),
  ).not.toBeNull();
});

test("renders unhealthy resources and preserves clock-driven schedule states", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-29T09:01:00"));
  const data = fixtureData("dashboard");
  data.dashboard.health = {
    healthy: false,
    unhealthy: { pods: [{ namespace: "synthetic", name: "pending-pod" }] },
  };
  await render("populated", data);
  expect(document.body.textContent).toContain("1 unhealthy");
  expect(document.body.textContent).toContain("synthetic/pending-pod");
  expect(document.querySelector(".event-row--past").textContent).toContain(
    "Synthetic past event",
  );
  expect(document.querySelector(".event-row--active").textContent).toContain(
    "Synthetic active event",
  );
  const before = document.querySelector(".clock").getAttribute("datetime");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: false })),
  );
  await vi.advanceTimersByTimeAsync(60000);
  await settle();
  expect(
    Date.parse(document.querySelector(".clock").getAttribute("datetime")) -
      Date.parse(before),
  ).toBe(60000);
});
test.each(["empty", "unavailable"])(
  "renders %s data without losing navigation",
  async (scenario) => {
    await render(scenario);
    expect(document.body.textContent).toContain(
      scenario === "empty" ? "a clear calendar" : "calendar unavailable",
    );
    expect(document.body.textContent).toContain("nothing due, go outside");
    expect(document.querySelectorAll(".tile")).toHaveLength(launcher.length);
  },
);
test("updates at 60 seconds, retains last-good on all failures and cleans up", async () => {
  vi.useFakeTimers();
  const next = fixtureData("dashboard").dashboard;
  next.health.scanned = 77;
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(response(next))
    .mockResolvedValueOnce({ ok: false })
    .mockResolvedValueOnce(response({ error: true }))
    .mockRejectedValueOnce(new Error("offline"));
  vi.stubGlobal("fetch", fetch);
  await render();
  await vi.advanceTimersByTimeAsync(59999);
  expect(fetch).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  await settle();
  expect(document.body.textContent).toContain("77 workloads scanned");
  for (let i = 0; i < 3; i++) {
    await vi.advanceTimersByTimeAsync(60000);
    await settle();
    expect(document.body.textContent).toContain("77 workloads scanned");
  }
  await unmount(instance);
  instance = null;
  await vi.advanceTimersByTimeAsync(120000);
  expect(fetch).toHaveBeenCalledTimes(4);
  expect(vi.getTimerCount()).toBe(0);
});
test("task toggle is optimistic, commits success and reverts refusal", async () => {
  let resolve;
  const fetch = vi.fn(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  await render();
  const button = document.querySelector(
    '[aria-label="Toggle Synthetic daily task"]',
  );
  button.click();
  await settle();
  expect(button.getAttribute("aria-pressed")).toBe("true");
  expect(fetch.mock.calls[0][1].body.get("status")).toBe("todo");
  resolve(response({ type: "success", data: { status: "done" } }));
  await settle();
  expect(button.getAttribute("aria-pressed")).toBe("true");
  button.click();
  await settle();
  expect(button.getAttribute("aria-pressed")).toBe("false");
  resolve(response({ type: "failure" }));
  await settle();
  expect(button.getAttribute("aria-pressed")).toBe("true");
});
test("chat parses split SSE frames, retains prior text history and handles errors", async () => {
  const stream = () =>
    new ReadableStream({
      start(controller) {
        for (const part of [
          'data: {"type":"text_',
          'chunk","data":{"text":"Synthetic response"}}\n\n',
          'data: {"type":"tool_call","data":{"tool":"fixture","args":{"name":"test"}}}\n\n',
        ])
          controller.enqueue(new TextEncoder().encode(part));
        controller.close();
      },
    });
  const fetch = vi
    .fn()
    .mockImplementationOnce(async () => ({ ok: true, body: stream() }))
    .mockResolvedValueOnce({ ok: false, status: 503 });
  vi.stubGlobal("fetch", fetch);
  await render();
  const input = document.querySelector('[aria-label="Ask the cluster"]');
  const send = async (text) => {
    input.value = text;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    input.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    );
    await settle();
  };
  await send("Synthetic question");
  expect(document.body.textContent).toContain("Synthetic response");
  expect(document.body.textContent).toContain("> fixture test");
  await send("Follow-up");
  expect(JSON.parse(fetch.mock.calls[1][1].body).history).toEqual([
    { role: "user", content: "Synthetic question" },
    { role: "assistant", content: "Synthetic response" },
  ]);
  expect(document.body.textContent).toContain("HTTP 503");
  expect(input.disabled).toBe(false);
});
test("scheme persists and is restored on remount against dark OS", async () => {
  vi.stubGlobal("matchMedia", () => ({
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  }));
  document.documentElement.dataset.theme = "light";
  await render();
  expect(document.querySelector(".scheme").getAttribute("aria-label")).toBe(
    "Switch to night scheme",
  );
  document.querySelector(".scheme").click();
  await settle();
  expect(localStorage.getItem("td-theme")).toBe("dark");
  await unmount(instance);
  instance = null;
  await render();
  expect(document.querySelector(".scheme").getAttribute("aria-label")).toBe(
    "Switch to day scheme",
  );
});
