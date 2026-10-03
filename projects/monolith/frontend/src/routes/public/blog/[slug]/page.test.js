// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Post from "./+page.svelte";

vi.mock("$lib/public/components", () => ({ Seo: () => {} }));
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
