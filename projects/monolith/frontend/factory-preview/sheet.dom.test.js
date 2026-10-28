import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Routes from "./SheetRoutes.svelte";
import { fixtureData, fixturePath, page } from "./sheet-state.svelte.js";
import { payloads } from "./fixtures.js";

let instance, target, observers;
beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  observers = [];
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor() {
        this.observe = vi.fn();
        this.disconnect = vi.fn();
        observers.push(this);
      }
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url) => {
      const values = payloads();
      if (!(url in values))
        throw new Error(`Unexpected fixture request: ${url}`);
      return new Response(JSON.stringify(values[url]));
    }),
  );
  window.scrollTo = vi.fn();
  Element.prototype.scrollIntoView = vi.fn();
  target = document.createElement("div");
  document.body.append(target);
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  for (const observer of observers)
    expect(observer.disconnect).toHaveBeenCalledOnce();
  target.remove();
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
  vi.unstubAllGlobals();
});
async function render(view, extra = {}) {
  const data = { ...fixtureData(view), ...extra };
  const path = fixturePath(view);
  page.url = new URL(
    path,
    view.startsWith("updates")
      ? "https://private.fixture.invalid"
      : "https://fixture.invalid",
  );
  page.params = { slug: data.slug };
  page.data = data;
  instance = mount(Routes, { target, props: { view, data } });
  await tick();
  return data;
}

it.each([
  "blog",
  "blog-empty",
  "post",
  "single",
  "landing",
  "overview",
  "context",
  "chapter",
  "search",
  "activity",
  "task",
  "attempt",
  "work-item",
  "updates",
  "updates-filter",
  "updates-empty",
  "updates-filter-empty",
])(
  "composes the production %s route with one sheet and no legacy nav",
  async (view) => {
    await render(view);
    expect(target.querySelectorAll("main.td.sheet")).toHaveLength(1);
    expect(target.querySelector(".md-nav")).toBeNull();
    expect(target.querySelector("h1").textContent.trim()).not.toBe("");
    expect(target.querySelector(".frame")).not.toBeNull();
    if (
      [
        "blog",
        "blog-empty",
        "post",
        "single",
        "landing",
        "context",
        "chapter",
        "search",
      ].includes(view) ||
      view.startsWith("updates")
    )
      expect(target.querySelector(".frame .journal")).not.toBeNull();
  },
);

it("preserves the TOC, single-column and special landing distinctions", async () => {
  await render("post");
  expect(target.querySelectorAll(".toc a")).toHaveLength(4);
  expect(target.querySelector(".post-body.prose")).not.toBeNull();
  expect(target.querySelector(".journal.single")).toBeNull();
  await unmount(instance);
  instance = undefined;
  await render("single");
  expect(target.querySelector(".toc")).toBeNull();
  expect(target.querySelector(".journal.single")).not.toBeNull();
  await unmount(instance);
  instance = undefined;
  await render("landing");
  const coding = [...target.querySelectorAll("button")].find((button) =>
    button.textContent.includes("Agentic coding"),
  );
  coding.click();
  await tick();
  expect(coding.getAttribute("aria-pressed")).toBe("true");
});

it("preserves updates query links and scoped observer cleanup across data changes", async () => {
  await render("updates-filter");
  const links = [...target.querySelectorAll("main a")].map((a) =>
    a.getAttribute("href"),
  );
  expect(links).toContain("/updates?month=2026-09&project=monolith");
  expect(
    links.some(
      (href) =>
        href.includes("technology=frontend") &&
        href.includes("project=monolith"),
    ),
  ).toBe(true);
  expect(target.querySelectorAll("[data-update-date]")).toHaveLength(5);
  expect(
    observers.some((observer) => observer.observe.mock.calls.length === 5),
  ).toBe(true);
});

it.each([
  ["updates-empty", "Waiting for the first daily edition"],
  ["updates-filter-empty", "No updates match these filters"],
])("keeps the %s empty state", async (view, text) => {
  await render(view);
  expect(target.textContent).toContain(text);
  expect(target.querySelector("[data-update-date]")).toBeNull();
});

it("keeps a stored explicit scheme authoritative when the OS changes", async () => {
  document.documentElement.dataset.theme = "light";
  await render("post");
  const toggle = target.querySelector("button.scheme");
  expect(toggle.getAttribute("aria-label")).toBe("Switch to night scheme");
  toggle.click();
  await tick();
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(localStorage.getItem("td-theme")).toBe("dark");
});
