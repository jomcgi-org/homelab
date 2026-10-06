// @vitest-environment happy-dom
import { expect, it, vi } from "vitest";
import { hydrate, tick, unmount } from "svelte";
import ThemeFixture from "./ThemeFixture.svelte";
import { renderFixture } from "./server-render.js";
import "@homelab/design-system/tokens/technical-drawing.css";

it("hydrates actual server markup without replacing nodes, text or attributes", async () => {
  const { body } = await renderFixture();
  const target = document.createElement("div");
  target.innerHTML = body;
  document.body.append(target);
  const elements = [...target.querySelectorAll("*")];
  const text = target.textContent;
  const attributes = () =>
    [...target.querySelectorAll("*")].map((node) =>
      [...node.attributes].map((attribute) => [
        attribute.name,
        attribute.value,
      ]),
    );
  const before = attributes();
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  let component;
  try {
    const light = target.querySelector('[data-sample="light"]');
    expect(light.dataset.dsTheme).toBe("technical-drawing-light");
    expect(light.querySelector('[data-sample="nested"]').dataset.dsTheme).toBe(
      "technical-drawing-dark",
    );
    expect(target.querySelector('[data-sample="dark"]').dataset.dsTheme).toBe(
      "technical-drawing-dark",
    );
    component = hydrate(ThemeFixture, { target, recover: false });
    await tick();
    expect(target.textContent).toBe(text);
    expect(attributes()).toEqual(before);
    expect([...target.querySelectorAll("*")]).toEqual(elements);
    expect(warn.mock.calls).toEqual([]);
    expect(error.mock.calls).toEqual([]);
    light.querySelector("button").click();
    await tick();
    expect(light.querySelector("button").textContent).toContain(
      "Sample action: 1",
    );
    expect(
      target.querySelector('[data-sample="dark"] button').textContent,
    ).toContain("Sample action: 0");
  } finally {
    if (component) await unmount(component);
    warn.mockRestore();
    error.mockRestore();
    target.remove();
  }
});
