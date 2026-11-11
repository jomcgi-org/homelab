// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../../test/SheetRoute.svelte";
import { leaderboard } from "../../../../../test/sheet-fixtures.js";
import { page, setPage } from "../../../../../test/profile-page.svelte.js";
import { replaceState } from "$app/navigation";

vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));
vi.mock("$app/navigation", () => ({ replaceState: vi.fn() }));

let instance;
let target;
const ranked = () =>
  [
    ...target.querySelectorAll(
      ".ranked.hovering tbody tr, .ranked:has(th[aria-sort]) tbody tr",
    ),
  ].map((row) => row.dataset.model);
const button = (text) =>
  [...target.querySelectorAll("button")].find((node) =>
    node.textContent.trim().startsWith(text),
  );
async function click(text) {
  button(text).click();
  await tick();
}

beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  target = document.createElement("div");
  document.body.append(target);
  vi.mocked(replaceState).mockImplementation((url) => {
    page.url = url;
  });
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
  vi.clearAllMocks();
});

async function render(query = "", report = leaderboard) {
  setPage(`https://jomcgi.dev/app/llm-leaderboard${query}`);
  instance = mount(Route, { target, props: { data: { leaderboard: report } } });
  await tick();
}

describe("leaderboard mounted with the production layouts", () => {
  it("composes the shared sheet, trail and one scheme control, preserving SEO and chart panels", async () => {
    await render();
    expect(
      target.querySelector("main.td.sheet.lb-page > .frame"),
    ).not.toBeNull();
    expect(target.querySelectorAll(".chrome")).toHaveLength(1);
    expect(target.querySelectorAll("button.scheme")).toHaveLength(1);
    expect(target.querySelector(".md-nav, .masthead, .spine")).toBeNull();
    expect(
      target.querySelector(".trail-dock .trail [aria-current=page]")
        .textContent,
    ).toBe("llm-leaderboard");
    expect(target.querySelectorAll(".grid2 > section")).toHaveLength(4);
    expect(target.querySelector(".matrix")).not.toBeNull();
    expect(document.title).toBe("LLM Leaderboard · jomcgi.dev");
    expect(document.querySelector('link[rel="canonical"]').href).toBe(
      "https://jomcgi.dev/app/llm-leaderboard",
    );
    target.querySelector("button.scheme").click();
    await tick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("td-theme")).toBe("dark");
  });

  it("reads m, ignores unknown ids, sorts both directions and shallowly replaces only the selection query", async () => {
    await render(
      "?keep=yes&m=anthropic%2Ffixture-anchor,deepseek%2Ffixture-cloud,unknown#task-1",
    );
    expect(ranked()).toEqual([
      "deepseek/fixture-cloud",
      "anthropic/fixture-anchor",
    ]);
    await click("$ / task");
    expect(ranked()).toEqual([
      "deepseek/fixture-cloud",
      "anthropic/fixture-anchor",
    ]);
    await click("$ / task");
    expect(ranked()).toEqual([
      "anthropic/fixture-anchor",
      "deepseek/fixture-cloud",
    ]);
    expect(
      target.querySelector('[aria-sort="descending"] button').textContent,
    ).toContain("$ / task");
    expect(replaceState).not.toHaveBeenCalled();
    await click("Self-hosted");
    const [url, state] = vi.mocked(replaceState).mock.lastCall;
    expect(url.pathname).toBe("/app/llm-leaderboard");
    expect(url.searchParams.get("keep")).toBe("yes");
    expect(url.hash).toBe("#task-1");
    expect(url.searchParams.get("m")).toBe(
      "anthropic/fixture-anchor,qwen/fixture-local",
    );
    expect(state).toEqual({});
    await click("Top picks");
    expect(vi.mocked(replaceState).mock.lastCall[0].searchParams.has("m")).toBe(
      false,
    );
  });

  it("supports empty selection, keyboard search and individual checkboxes without changing preset semantics", async () => {
    await render();
    await click("Clear all");
    expect(ranked()).toEqual([]);
    expect(target.querySelector(".empty").textContent).toContain(
      "No models selected.",
    );
    // Existing behaviour: clearing removes m; reloading uses the default preset.
    expect(vi.mocked(replaceState).mock.lastCall[0].searchParams.has("m")).toBe(
      false,
    );
    const search = target.querySelector('input[type="search"]');
    search.value = "deepseek";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    expect(target.querySelector("details").open).toBe(true);
    search.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    );
    await tick();
    expect(ranked()).toEqual(["deepseek/fixture-cloud"]);
    target.querySelector('input[type="checkbox"]').click();
    await tick();
    expect(ranked()).toEqual([]);
    search.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    await tick();
    expect(search.value).toBe("");
    expect(target.querySelectorAll('input[type="checkbox"]')).toHaveLength(4);
  });

  it("keeps factory-role selection and linked chart hover interactions", async () => {
    await render();
    await click("reviewer");
    expect(button("reviewer").getAttribute("aria-pressed")).toBe("true");
    expect(target.textContent).toContain("0.600");
    const row = target.querySelector(
      '.ranked:has(th[aria-sort]) [data-model="deepseek/fixture-cloud"]',
    );
    row.dispatchEvent(new MouseEvent("mouseenter"));
    await tick();
    expect(
      target.querySelectorAll('[data-model="deepseek/fixture-cloud"].hot')
        .length,
    ).toBeGreaterThan(1);
    row.dispatchEvent(new MouseEvent("mouseleave"));
    await tick();
    expect(target.querySelector(".hot")).toBeNull();
  });

  it.each([{}, { models: [], tasks: [] }])(
    "renders an empty report without chart or sort failures",
    async (report) => {
      await render("?m=unknown", report);
      expect(target.querySelector("main.sheet")).not.toBeNull();
      expect(target.querySelector(".empty").textContent).toContain(
        "No models selected.",
      );
      expect(target.querySelectorAll(".tasks li")).toHaveLength(0);
      await click("All");
      expect(ranked()).toEqual([]);
    },
  );
});
