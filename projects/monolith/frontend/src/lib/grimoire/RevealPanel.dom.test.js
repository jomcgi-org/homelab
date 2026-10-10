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
// A closed drawer stays mounted but hidden, so it is not a dialog to a reader.
const dialog = () => document.querySelector('[role="dialog"]:not([hidden])');

describe("reveal drawer", () => {
  it("opens a labelled dialog with focus inside and no editor until opened", async () => {
    await render();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.body.textContent).not.toContain("Find knowledge");
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

  it("keeps the editor state across a close and reopen", async () => {
    await render();
    opener().click();
    await settle();
    const input = dialog().querySelector("input");
    input.value = "Mara";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    buttonByText(dialog(), "Close reveal panel").click();
    await settle();
    opener().click();
    await settle();
    expect(dialog().querySelector("input").value).toBe("Mara");
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
