// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { hydrate, tick, unmount } from "svelte";
import GalleryFixture from "./GalleryFixture.svelte";
import { renderFixture } from "./server-render.js";

let mounted;
let target;
afterEach(async () => {
  if (mounted) await unmount(mounted);
  target?.remove();
  mounted = undefined;
  vi.restoreAllMocks();
});

it("hydrates unchanged SSR nodes and associations, then repeats local interactions", async () => {
  const { body } = await renderFixture();
  target = document.createElement("div");
  target.innerHTML = body;
  document.body.append(target);
  const nodes = [...target.querySelectorAll("*")];
  const snapshot = () => ({
    text: target.textContent,
    attributes: [...target.querySelectorAll("*")].map((node) =>
      [...node.attributes].map(({ name, value }) => [name, value]),
    ),
  });
  const before = snapshot();
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  mounted = hydrate(GalleryFixture, { target, recover: false });
  await tick();
  expect([...target.querySelectorAll("*")]).toEqual(nodes);
  expect(snapshot()).toEqual(before);
  const ids = [...target.querySelectorAll("[id]")].map((node) => node.id);
  expect(new Set(ids).size).toBe(ids.length);
  for (const label of target.querySelectorAll("label"))
    expect(target.querySelector(`[id="${label.htmlFor}"]`)).not.toBeNull();
  for (const node of target.querySelectorAll(
    "[aria-labelledby], [aria-describedby]",
  )) {
    for (const attr of ["aria-labelledby", "aria-describedby"]) {
      for (const id of node.getAttribute(attr)?.split(" ") ?? [])
        expect(target.querySelector(`[id="${id}"]`)).not.toBeNull();
    }
  }
  for (const boundary of ["light", "dark"]) {
    const root = target.querySelector(`[data-gallery-boundary="${boundary}"]`);
    const nav = root.querySelector('[data-gallery-section="navigation"]');
    const tabs = [...nav.querySelectorAll('[role="tab"]')];
    for (let repeat = 0; repeat < 2; repeat++) {
      tabs[0].dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "ArrowRight",
          bubbles: true,
          cancelable: true,
        }),
      );
      await tick();
      expect(tabs[1].getAttribute("aria-selected")).toBe("true");
      expect(tabs[2].disabled).toBe(true);
      tabs[1].dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "End",
          bubbles: true,
          cancelable: true,
        }),
      );
      await tick();
      expect(tabs[1].getAttribute("aria-selected")).toBe("true");
      tabs[1].dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Home",
          bubbles: true,
          cancelable: true,
        }),
      );
      await tick();
      expect(tabs[0].getAttribute("aria-selected")).toBe("true");
      const disclosure = root.querySelector(
        `[data-gallery-action="${boundary}-disclosure"]`,
      );
      disclosure.click();
      await tick();
      expect(
        root.querySelector('[data-gallery-state="disclosure"]').textContent,
      ).toContain("true");
      disclosure.click();
      await tick();
      expect(
        root.querySelector('[data-gallery-state="disclosure"]').textContent,
      ).toContain("false");
    }
    for (const control of root.querySelectorAll('[aria-invalid="true"]')) {
      expect(control.required).toBe(true);
      const description = control
        .getAttribute("aria-describedby")
        .split(" ")
        .map((id) => target.querySelector(`[id="${id}"]`).textContent)
        .join(" ");
      expect(description).toContain(
        "Error: Reference must contain a sheet label",
      );
    }
    const count = root.querySelector(
      `[data-gallery-action="${boundary}-refresh"]`,
    );
    const beforeDisabled = count.textContent;
    root
      .querySelector(`[data-gallery-action="${boundary}-controls-disabled"]`)
      .click();
    await tick();
    expect(count.textContent).toBe(beforeDisabled);
    count.click();
    await tick();
    expect(count.textContent).not.toBe(beforeDisabled);
    root
      .querySelector("form")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await tick();
    expect(
      root.querySelector('[data-gallery-state="submissions"]').textContent,
    ).not.toBe("Submissions: 0");
    for (const table of root.querySelectorAll("[data-gallery-fallback]")) {
      table.closest("details").querySelector("summary").click();
      expect(table.closest("details").open).toBe(true);
      expect(table.querySelector("caption").textContent).toContain("MiB");
      expect(table.querySelectorAll("tbody tr")).toHaveLength(5);
    }
  }
  expect(warn.mock.calls).toEqual([]);
  expect(error.mock.calls).toEqual([]);
});
