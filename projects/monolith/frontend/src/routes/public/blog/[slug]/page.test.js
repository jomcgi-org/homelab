// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Post from "./+page.svelte";

vi.mock("$lib/public/components", () => ({ Seo: () => {} }));
vi.mock("$app/navigation", () => ({ replaceState: vi.fn() }));
vi.stubGlobal(
  "IntersectionObserver",
  class {
    observe() {}
    disconnect() {}
  },
);
const data = {
  slug: "125b-on-a-4090",
  title: "Serving larger-than-memory MoE models",
  date: "2026-09-01",
  summary: "The author's original summary.",
  preamble: "",
  sections: [
    '<h2 id="inference-demo">Inference Demo</h2>',
    '<h2 id="1-freetoken">1. FreeToken</h2><p>The author’s original explanation.</p>',
  ],
  toc: [
    { id: "inference-demo", text: "Inference Demo", children: [] },
    { id: "1-freetoken", text: "1. FreeToken", children: [] },
  ],
};
let component;
let target;
afterEach(async () => {
  if (component) await unmount(component);
  target?.remove();
  delete document.documentElement.dataset.theme;
  localStorage.removeItem("td-theme");
});
async function render(props) {
  target = document.createElement("div");
  document.body.append(target);
  component = mount(Post, { target, props: { data: props } });
  await tick();
}
test("the 4090 demo opens before the article and its navigation, preserving author content", async () => {
  await render(data);
  await vi.waitFor(() =>
    expect(target.querySelector(".demo-landing .replay")).not.toBeNull(),
  );
  expect(target.querySelector(".demo-landing .spine")).toBeNull();
  expect(target.querySelector(".journal .replay")).toBeNull();
  expect(target.querySelectorAll("h1")).toHaveLength(1);
  expect(target.querySelector("h1").textContent).toBe("125B on a 4090");
  expect(target.querySelectorAll(".landing-header nav")).toHaveLength(1);
  expect(target.querySelector(".landing-header .trail")).toBeNull();
  expect(target.querySelector(".landing-header time")).toBeNull();
  expect(
    target.querySelector(".post-frame time").getAttribute("datetime"),
  ).toBe(data.date);
  expect(target.querySelector(".post-frame h2").textContent).toBe(data.title);
  expect(target.querySelectorAll("#inference-demo")).toHaveLength(1);
  expect(target.querySelector(".post-frame").textContent).toContain(
    data.summary,
  );
  expect(target.querySelector(".post-frame").textContent).toContain(
    "The author’s original explanation.",
  );
  expect(target.querySelector(".read-post").getAttribute("href")).toBe(
    "#post-body",
  );
  expect(target.querySelector("#post-body .spine")).not.toBeNull();
});

test("Read the post moves keyboard focus to the article", async () => {
  await render(data);
  const article = target.querySelector("#post-body");
  article.scrollIntoView = vi.fn();
  target.querySelector(".read-post").click();
  expect(article.scrollIntoView).toHaveBeenCalled();
  expect(document.activeElement).toBe(article);
});

test("the compact header retains the persistent day and night switch", async () => {
  await render(data);
  const toggle = target.querySelector(".landing-header .scheme");
  toggle.click();
  await tick();
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(localStorage.getItem("td-theme")).toBe("dark");
  expect(toggle.getAttribute("aria-label")).toBe("Switch to day scheme");
  toggle.click();
  await tick();
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(localStorage.getItem("td-theme")).toBe("light");
});
test("other posts keep their title, summary and sections alongside navigation", async () => {
  await render({ ...data, slug: "another-post" });
  expect(target.querySelector(".demo-landing")).toBeNull();
  expect(target.querySelector(".post-frame h1").textContent).toBe(data.title);
  expect(target.querySelector(".post-frame").textContent).toContain(
    data.summary,
  );
  expect(target.querySelector(".post-frame #inference-demo")).not.toBeNull();
  expect(target.querySelector(".journal .spine")).not.toBeNull();
});

test("posts without an index retain their inline breadcrumb and conformance recording", async () => {
  await render({
    ...data,
    slug: "ember-conformance",
    toc: [],
    sections: [],
    preamble: "<p>Conformance post introduction.</p>",
  });
  await vi.waitFor(() =>
    expect(
      target.querySelector(
        '[aria-label="Trace conformance test, one recorded run"]',
      ),
    ).not.toBeNull(),
  );
  expect(target.querySelector(".journal.single .trail-inline")).not.toBeNull();
  expect(target.querySelector(".spine")).toBeNull();
  expect(target.querySelector(".post-frame").textContent).toContain(
    "Conformance post introduction.",
  );
  expect(
    target.querySelector(".post-frame .ed-lead").textContent,
  ).not.toContain(data.summary);
});
