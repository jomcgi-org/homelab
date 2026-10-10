// @vitest-environment happy-dom
import { afterEach, expect, test, vi } from "vitest";
import { createRawSnippet, mount, tick, unmount } from "svelte";
import Layout from "./+layout.svelte";

const { page } = vi.hoisted(() => ({
  page: { params: {}, data: {} },
}));
vi.mock("$app/state", () => ({ page }));
vi.mock("$lib/public/components", async () => ({
  TechnicalDrawingChrome: (
    await import("$lib/public/components/TechnicalDrawingChrome.svelte")
  ).default,
}));

let component;
let target;
afterEach(async () => {
  if (component) await unmount(component);
  target?.remove();
});

async function render(slug) {
  page.params = { slug };
  page.data = { title: "Post title" };
  target = document.createElement("div");
  document.body.append(target);
  component = mount(Layout, {
    target,
    props: {
      children: createRawSnippet(() => ({
        render: () => "<p>Post content</p>",
      })),
    },
  });
  await tick();
}

test("the 4090 landing supplies its only header", async () => {
  await render("125b-on-a-4090");
  expect(target.querySelector(".chrome")).toBeNull();
  expect(target.textContent.trim()).toBe("Post content");
});

test.each([undefined, "ember-conformance"])(
  "the blog and other posts retain their normal chrome (%s)",
  async (slug) => {
    await render(slug);
    expect(target.querySelectorAll(".chrome")).toHaveLength(1);
    expect(target.querySelectorAll(".scheme")).toHaveLength(1);
    expect(target.querySelector('.trail a[href="/blog"]')).not.toBeNull();
  },
);
