// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import RevealPanel from "./RevealPanel.svelte";
import { buttonByText, settle } from "./test-helpers.js";

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function render() {
  vi.stubGlobal("fetch", vi.fn());
  instance = mount(RevealPanel, {
    target: document.body,
    props: {
      endpoint: "/state",
      characters: [{ id: "pc", character_name: "Aria" }],
      changed: vi.fn(),
    },
  });
  await settle();
}
const opener = () => buttonByText(document.body, "Reveal knowledge");
const dialog = () => document.querySelector('[role="dialog"]');

describe("reveal drawer", () => {
  it("opens a labelled dialog with focus inside and no editor until opened", async () => {
    await render();
    expect(dialog()).toBeNull();
    expect(document.body.textContent).not.toContain("Knowledge scope");
    opener().click();
    await settle();
    expect(dialog().getAttribute("aria-labelledby")).toBe("reveal-panel-title");
    expect(document.getElementById("reveal-panel-title").textContent).toBe(
      "Reveal to your players",
    );
    expect(dialog().contains(document.activeElement)).toBe(true);
    expect(dialog().textContent).toContain("Find knowledge");
  });

  it("closes on Escape and returns focus to the opener", async () => {
    await render();
    opener().click();
    await settle();
    dialog().dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    await settle();
    expect(dialog()).toBeNull();
    expect(document.activeElement).toBe(opener());
  });

  it("closes from the close button and returns focus to the opener", async () => {
    await render();
    opener().click();
    await settle();
    buttonByText(dialog(), "Close reveal panel").click();
    await settle();
    expect(dialog()).toBeNull();
    expect(document.activeElement).toBe(opener());
  });
});
