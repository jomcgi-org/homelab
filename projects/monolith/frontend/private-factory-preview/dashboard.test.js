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
  vi.restoreAllMocks();
});
const headings = () =>
  [...document.querySelectorAll("h2")].map((node) =>
    node.textContent.replace(/\s+/g, " ").trim(),
  );
const keyFor = (label) =>
  [...document.querySelectorAll(".key")].find(
    (node) =>
      node.textContent.replace(/\s+/g, " ").trim().split(" ")[0] ===
        label.split(" ")[0] && node.textContent.includes(label),
  );
test("preserves sections, keypad destinations and deduplicated daily tasks", async () => {
  await render();
  expect(headings()).toEqual([
    "Needs you 2",
    "Today",
    "Tasks",
    "Shipping",
    "Ask the cluster",
  ]);
  const conductor = fixtureData("dashboard").dashboard.factory.conductor.href;
  for (const item of launcher) {
    const link = keyFor(item.label);
    expect(link, item.label).toBeTruthy();
    expect(link.getAttribute("href")).toBe(
      item.dynamic === "conductor" ? conductor : item.href,
    );
    expect(link.getAttribute("target")).toBe(item.external ? "_blank" : null);
  }
  expect(keyFor("decisions").querySelector(".num").textContent).toBe("2");
  expect(document.querySelectorAll(".keypad .key")).toHaveLength(
    launcher.length,
  );
  expect(
    document.querySelectorAll('[aria-label="Toggle Synthetic daily task"]'),
  ).toHaveLength(1);
  expect(document.querySelectorAll(".task-row")).toHaveLength(2);
  expect(document.querySelector(".back-to-dashboard")).toBeNull();
  expect(document.querySelectorAll(".scheme")).toHaveLength(1);
});
test("lists the open decisions first and reads the factory lane", async () => {
  await render();
  const rows = [...document.querySelectorAll(".needs-list .row")].map(
    (row) => ({
      kind: row.querySelector("dt").textContent,
      href: row.querySelector("a")?.getAttribute("href") ?? null,
      text: row.querySelector("dd").textContent.replace(/\s+/g, " ").trim(),
    }),
  );
  expect(rows).toHaveLength(2);
  expect(rows[0].kind).toBe("decision");
  expect(rows[0].href).toBe("/factory/escalations");
  expect(rows[0].text).toContain("#90031 Synthetic 31");
  expect(rows[0].text).toContain(
    "Which synthetic scope should this receipt record?",
  );
  expect(document.querySelector(".pulse").textContent).toContain(
    "factory enabled · 1 in flight · 1 queued",
  );
  expect(document.querySelector(".pulse a").getAttribute("href")).toBe(
    "/factory",
  );
});
test("puts a red PR and an unhealthy workload on the list, after decisions", async () => {
  const data = fixtureData("dashboard");
  data.dashboard.health = {
    healthy: false,
    unhealthy: { pods: [{ namespace: "synthetic", name: "pending-pod" }] },
  };
  data.dashboard.github.open_prs[0].ci = "failing";
  await render("populated", data);
  expect(headings()[0]).toBe("Needs you 4");
  const kinds = [...document.querySelectorAll(".needs-list dt")].map(
    (node) => node.textContent,
  );
  expect(kinds).toEqual(["decision", "decision", "unhealthy", "ci failed"]);
  const ci = [...document.querySelectorAll(".needs-list a")].at(-1);
  expect(ci.getAttribute("href")).toBe("https://example.test/pull/90001");
  expect(ci.getAttribute("target")).toBe("_blank");
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
test("says which readings did not answer and marks a held lane amber", async () => {
  const data = fixtureData("dashboard", "empty");
  data.dashboard = {
    factory: { ...data.dashboard.factory, state: "paused" },
  };
  await render("empty", data);
  expect(document.querySelector(".needs .none").textContent.trim()).toBe(
    "Nothing waiting in what answered. No reading from cluster, github.",
  );
  const dot = document.querySelector(".pulse a .pulse-dot");
  expect(dot.classList.contains("pulse-dot--warn")).toBe(true);
  expect(document.querySelector(".pulse a").textContent).toContain(
    "factory paused",
  );
});
test.each(["empty", "unavailable"])(
  "renders %s data without losing navigation",
  async (scenario) => {
    await render(scenario);
    expect(document.body.textContent).toContain(
      scenario === "empty" ? "a clear calendar" : "calendar unavailable",
    );
    expect(document.body.textContent).toContain("nothing due, go outside");
    expect(headings()[0]).toBe("Needs you 0");
    expect(document.querySelector(".needs .none").textContent.trim()).toBe(
      scenario === "empty"
        ? "Nothing is waiting on you."
        : "No reading. The dashboard is unavailable.",
    );
    expect(document.querySelector(".pulse").textContent).toContain(
      scenario === "empty"
        ? "factory enabled · 0 in flight · 0 queued"
        : "factory unavailable",
    );
    expect(document.querySelectorAll(".keypad .key")).toHaveLength(
      launcher.length,
    );
    expect(keyFor("decisions").querySelector(".num")).toBeNull();
    expect(keyFor("conductor").getAttribute("href")).toBe(
      scenario === "empty"
        ? fixtureData("dashboard", "empty").dashboard.factory.conductor.href
        : "/factory/execution?compose=1",
    );
  },
);
test("updates at 60 seconds, ages out a dead backend and cleans up", async () => {
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
  // Refused, {error} and offline: the last good read holds for two misses,
  // then the page says the readings are gone instead of showing them green.
  for (let i = 0; i < 2; i++) {
    await vi.advanceTimersByTimeAsync(60000);
    await settle();
    expect(document.body.textContent).toContain("77 workloads scanned");
  }
  await vi.advanceTimersByTimeAsync(60000);
  await settle();
  expect(document.body.textContent).not.toContain("77 workloads scanned");
  expect(document.body.textContent).toContain("cluster status unavailable");
  expect(document.body.textContent).toContain("factory unavailable");
  expect(document.querySelector(".needs .none").textContent).toContain(
    "No reading from cluster, factory, github.",
  );
  await unmount(instance);
  instance = null;
  await vi.advanceTimersByTimeAsync(120000);
  expect(fetch).toHaveBeenCalledTimes(4);
  expect(vi.getTimerCount()).toBe(0);
});
test("reads on return to the page, at most every 30 seconds, and stops on unmount", async () => {
  vi.useFakeTimers();
  const next = fixtureData("dashboard").dashboard;
  next.health.scanned = 91;
  const fetch = vi.fn().mockResolvedValue(response(next));
  vi.stubGlobal("fetch", fetch);
  const visible = () => document.dispatchEvent(new Event("visibilitychange"));
  await render();
  // The page just loaded: an immediate return does not read again.
  visible();
  await settle();
  expect(fetch).not.toHaveBeenCalled();
  // Hidden transitions never read.
  await vi.advanceTimersByTimeAsync(30000);
  const state = vi
    .spyOn(document, "visibilityState", "get")
    .mockReturnValue("hidden");
  visible();
  await settle();
  expect(fetch).not.toHaveBeenCalled();
  // A return 30 seconds on reads now; a second return straight after does not.
  state.mockReturnValue("visible");
  visible();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toBe("/dashboard-data");
  expect(document.body.textContent).toContain("91 workloads scanned");
  visible();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(1);
  // The listener goes with the page.
  await unmount(instance);
  instance = null;
  await vi.advanceTimersByTimeAsync(30000);
  visible();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(1);
});
test("runs one refresh at a time and bounds each with a timeout", async () => {
  vi.useFakeTimers();
  let resolve;
  const fetch = vi.fn(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  await render();
  await vi.advanceTimersByTimeAsync(60000);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  // The first request is still pending: neither a return to the page nor
  // the next tick starts a second one.
  await vi.advanceTimersByTimeAsync(30000);
  document.dispatchEvent(new Event("visibilitychange"));
  await vi.advanceTimersByTimeAsync(30000);
  expect(fetch).toHaveBeenCalledTimes(1);
  // Once it settles, the next tick reads again.
  resolve(response(fixtureData("dashboard").dashboard));
  await settle();
  await vi.advanceTimersByTimeAsync(60000);
  expect(fetch).toHaveBeenCalledTimes(2);
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
  expect(document.querySelector(".task-error")).toBeNull();
  resolve(response({ type: "failure" }));
  await settle();
  expect(button.getAttribute("aria-pressed")).toBe("true");
  const alert = document.querySelector(".task-error");
  expect(alert.getAttribute("role")).toBe("alert");
  expect(alert.textContent).toBe("Could not update Synthetic daily task.");
  button.click();
  await settle();
  expect(document.querySelector(".task-error")).toBeNull();
});
test("speaks each PR's CI state beside its dot", async () => {
  await render();
  const row = document.querySelector(".pr-row");
  expect(row.querySelector(".ci-dot").getAttribute("aria-hidden")).toBe("true");
  expect(row.querySelector(".sr-only").textContent).toBe("ci passing");
  expect(row.querySelector(".ci-dot--ok")).not.toBeNull();
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
