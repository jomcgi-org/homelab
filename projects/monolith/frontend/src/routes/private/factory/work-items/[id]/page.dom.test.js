// @vitest-environment happy-dom
import { afterEach, expect, it } from "vitest";
import { mount, unmount, tick } from "svelte";
import Page from "./+page.svelte";

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  document.body.innerHTML = "";
});

it("uses the sibling factory tabs without a missing work-items index", async () => {
  instance = mount(Page, {
    target: document.body,
    props: { data: { itemId: "missing", missing: true } },
  });
  await tick();
  const links = [
    ...document.querySelectorAll('nav[aria-label="Factory views"] a'),
  ];
  expect(
    links.map((link) => [link.getAttribute("href"), link.textContent.trim()]),
  ).toEqual([
    ["/factory", "factory"],
    ["/factory/escalations", "escalations"],
    ["/factory/execution", "work"],
    ["/factory/execution/drain", "knowledge extraction queue"],
  ]);
  expect(
    links.filter((link) => link.getAttribute("aria-current") === "page"),
  ).toEqual([links[0]]);
  expect(links[0].classList.contains("here")).toBe(true);
  expect(document.querySelector('a[href="/factory/work-items"]')).toBeNull();
});
