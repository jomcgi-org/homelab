import { afterEach, expect, test, vi } from "vitest";
import { mount, unmount, tick } from "svelte";
import App from "./App.svelte";
import { fixtureData, context, routes } from "./fixtures.js";
import { page, setRoute } from "./state.js";

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  document.body.innerHTML = "";
  delete document.documentElement.dataset.theme;
  localStorage.clear();
  vi.unstubAllGlobals();
});
const render = async (view, scenario = "populated") => {
  setRoute(view);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url) => {
      if (!/^\/factory\/escalations\/context\/(31|32)$/.test(url))
        throw new Error(`Unexpected fetch: ${url}`);
      return { ok: true, json: async () => context(31) };
    }),
  );
  instance = mount(App, {
    target: document.body,
    props: { view, data: fixtureData(view, scenario) },
  });
  await tick();
};

for (const view of Object.keys(routes)) {
  for (const scenario of [
    "populated",
    "empty",
    "unavailable",
    ...(view === "item" ? ["missing", "tracking"] : []),
  ]) {
    test(`${view} ${scenario}: one sheet/chrome with real tier layouts`, async () => {
      await render(view, scenario);
      expect(document.querySelectorAll("main.td.sheet")).toHaveLength(1);
      expect(document.querySelectorAll(".chrome .scheme")).toHaveLength(1);
      expect(document.querySelector(".back-to-dashboard")).toBeNull();
      expect(document.querySelector(".nav")).toBeNull();
      expect(
        document.querySelector('nav[aria-label="Factory views"]'),
      ).not.toBeNull();
      if (scenario === "unavailable")
        expect(document.querySelector("main").textContent).toContain(
          "unavailable",
        );
      if (scenario === "missing")
        expect(document.querySelector("main").textContent).toContain(
          "No work item 100001.",
        );
    });
  }
  test(`${view}: shared explicit scheme persists and remounts`, async () => {
    document.documentElement.dataset.theme = "light";
    await render(view);
    document.querySelector(".scheme").click();
    await tick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("td-theme")).toBe("dark");
    await unmount(instance);
    await render(view);
    expect(document.querySelector(".scheme").getAttribute("aria-label")).toBe(
      "Switch to day scheme",
    );
    document.querySelector(".scheme").click();
    await tick();
    expect(localStorage.getItem("td-theme")).toBe("light");
  });
}

test("tracking detail has source and owner without factory controls", async () => {
  await render("item", "tracking");
  expect(document.body.textContent).toContain("tracking only");
  expect(document.body.textContent).toContain(
    "Synthetic human and external coordination owner",
  );
  expect(document.querySelector("form")).toBeNull();
  expect(document.querySelector('[aria-label="Receipts"]')).toBeNull();
  expect(document.querySelector('[aria-label="Work item edges"]')).toBeNull();
});

test("board shows tracking alongside factory work without synthetic execution", async () => {
  await render("board");
  const rows = document.querySelectorAll('[aria-label="Tracking-only work"]');
  expect(rows).toHaveLength(2);
  expect(rows[1].textContent).toContain("Recorded external completion");
  for (const row of rows) {
    expect(row.querySelector("button,form,[role=progressbar]")).toBeNull();
    expect(row.textContent).not.toContain("receipt");
  }
});

test.each([
  "/factory/execution",
  "/factory/execution/drain",
  "/factory/escalations-extra",
  "/factory/work-items",
  "/updates",
])("keeps legacy dashboard navigation on %s", async (path) => {
  page.set({
    url: new URL(`https://private.example.test${path}`),
    data: {},
    error: null,
  });
  instance = mount(App, {
    target: document.body,
    props: { view: "legacy", data: {} },
  });
  await tick();
  expect(
    document.querySelector(".back-to-dashboard").getAttribute("href"),
  ).toBe("/");
  expect(document.querySelector(".scheme")).toBeNull();
});
