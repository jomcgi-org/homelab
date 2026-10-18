// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";
import YourTurnBanner from "./YourTurnBanner.svelte";
import { settle } from "./test-helpers.js";

const mine = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const other = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const entries = [
  { label: "Aria", player_character_id: mine, initiative: 19, hidden: false },
  { label: "Bram", player_character_id: other, initiative: 15, hidden: false },
  { label: "???", player_character_id: null, initiative: null, hidden: true },
  { label: "Goblin", player_character_id: null, initiative: 8, hidden: false },
];

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
});

async function render(activeIndex, extra = {}) {
  instance = mount(YourTurnBanner, {
    target: document.body,
    props: { entries, activeIndex, characterIds: [mine], ...extra },
  });
  await settle();
}
const region = () => document.querySelector('[role="status"]');

describe("your turn banner", () => {
  it("is shown when the active entry is the viewer's character", async () => {
    await render(0);
    expect(region().textContent).toContain("Your turn");
    expect(region().getAttribute("aria-live")).toBe("polite");
  });

  it("is hidden when another player's character is active", async () => {
    await render(1);
    expect(document.body.textContent).not.toContain("Your turn");
  });

  it("is hidden for a hidden NPC and for a plain NPC", async () => {
    await render(2);
    expect(document.body.textContent).not.toContain("Your turn");
    await unmount(instance);
    document.body.innerHTML = "";
    await render(3);
    expect(document.body.textContent).not.toContain("Your turn");
  });

  it("is hidden when no entry is active", async () => {
    await render(null);
    expect(document.body.textContent).not.toContain("Your turn");
  });

  it("never fires for a hidden entry that names the viewer's character", async () => {
    instance = mount(YourTurnBanner, {
      target: document.body,
      props: {
        entries: [{ ...entries[0], hidden: true }],
        activeIndex: 0,
        characterIds: [mine],
      },
    });
    await settle();
    expect(document.body.textContent).not.toContain("Your turn");
  });

  it("is hidden when the viewer owns no character in the order", async () => {
    await render(0, { characterIds: [other] });
    expect(document.body.textContent).not.toContain("Your turn");
  });

  it("keeps the live region mounted while empty so the change is announced", async () => {
    await render(1);
    expect(region()).not.toBeNull();
    expect(region().textContent.trim()).toBe("");
  });
});
