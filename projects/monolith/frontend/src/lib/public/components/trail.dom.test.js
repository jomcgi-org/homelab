// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";
import Trail from "./Trail.svelte";

let instance;
let target;

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target?.remove();
});

async function render(props) {
  target = document.createElement("div");
  document.body.append(target);
  instance = mount(Trail, { target, props });
  await tick();
  return target;
}

const crumbs = (root) =>
  [...root.querySelectorAll(".crumb")].map((el) => ({
    tag: el.tagName,
    text: el.textContent.trim(),
    href: el.getAttribute("href"),
    current: el.getAttribute("aria-current"),
    title: el.classList.contains("title"),
  }));

describe("Trail", () => {
  it("names a different root for a host whose / is not the public site", async () => {
    const root = await render({
      root: { label: "dashboard", href: "/" },
      crumbs: [{ label: "factory", href: "/factory" }],
    });
    expect(crumbs(root)[0]).toEqual({
      tag: "A",
      text: "dashboard",
      href: "/",
      current: null,
      title: false,
    });
  });

  it("marks the site root current when there is nowhere deeper", async () => {
    const root = await render({});
    expect(crumbs(root)).toEqual([
      {
        tag: "A",
        text: "jomcgi.dev",
        href: "/",
        current: "page",
        title: false,
      },
    ]);
  });

  it("links every crumb that has a destination and marks the last one current", async () => {
    const root = await render({
      crumbs: [{ label: "slop", href: "/slop" }, { label: "factory" }],
    });
    expect(crumbs(root)).toEqual([
      { tag: "A", text: "jomcgi.dev", href: "/", current: null, title: false },
      { tag: "A", text: "slop", href: "/slop", current: null, title: false },
      {
        tag: "SPAN",
        text: "factory",
        href: null,
        current: "page",
        title: false,
      },
    ]);
  });

  it("keeps a section crumb a link to itself when it is the current page", async () => {
    const root = await render({ crumbs: [{ label: "blog", href: "/blog" }] });
    const blog = root.querySelector('.trail a[href="/blog"]');
    expect(blog.getAttribute("aria-current")).toBe("page");
    expect(blog.classList.contains("current")).toBe(true);
  });

  it("gives a page title its own row below the sections", async () => {
    const root = await render({
      crumbs: [{ label: "blog", href: "/blog" }],
      current: "Formal verification at home",
    });
    const all = crumbs(root);
    expect(all.at(-1)).toEqual({
      tag: "SPAN",
      text: "Formal verification at home",
      href: null,
      current: "page",
      title: true,
    });
    expect(
      root
        .querySelector('.trail-row a[href="/blog"]')
        .getAttribute("aria-current"),
    ).toBeNull();
    expect(root.querySelector(".trail-row .title")).toBeNull();
  });

  it("is labelled as navigation", async () => {
    const root = await render({ crumbs: [{ label: "docs", href: "/docs" }] });
    expect(root.querySelector("nav").getAttribute("aria-label")).toBe(
      "You are here",
    );
  });
});
