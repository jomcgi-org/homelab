// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";
import { mount, unmount } from "svelte";
import TurnStrip from "./TurnStrip.svelte";
import { settle } from "./test-helpers.js";

const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SECRET = "Lich King Vorrath";

// The DM view: real labels for hidden entries.
const dmEntries = [
  { label: "Aria", player_character_id: pcA, initiative: 19, hidden: false },
  { label: SECRET, player_character_id: null, initiative: 14, hidden: true },
  { label: "Goblin", player_character_id: null, initiative: 8, hidden: false },
];
// The player projection of the same order.
const playerEntries = [
  dmEntries[0],
  { label: "???", player_character_id: null, initiative: null, hidden: true },
  dmEntries[2],
];

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
});

async function render(props) {
  instance = mount(TurnStrip, { target: document.body, props });
  await settle();
}
const items = () => [...document.querySelectorAll("li")];
const names = () =>
  items().map((item) => item.querySelector(".name").textContent);
const active = () =>
  items().filter((item) => item.getAttribute("aria-current") === "step");

describe("turn strip", () => {
  it("renders the labels in order with the round number", async () => {
    await render({ entries: dmEntries, round: 3, activeIndex: 0, dm: true });
    expect(names()).toEqual(["Aria", SECRET, "Goblin"]);
    expect(document.body.textContent).toContain("Round 3");
    expect(document.querySelector("section").getAttribute("aria-label")).toBe(
      "Turn order",
    );
  });

  it("marks the active entry with aria-current and a visible text cue", async () => {
    await render({ entries: dmEntries, round: 1, activeIndex: 2, dm: true });
    expect(active()).toHaveLength(1);
    expect(items().indexOf(active()[0])).toBe(2);
    expect(active()[0].textContent).toContain("Active turn");
    expect(document.body.textContent.match(/Active turn/g)).toHaveLength(1);
  });

  it("moves the marker when the active index changes", async () => {
    await render({ entries: dmEntries, round: 1, activeIndex: 0, dm: true });
    expect(items().indexOf(active()[0])).toBe(0);
    await unmount(instance);
    document.body.innerHTML = "";
    await render({ entries: dmEntries, round: 1, activeIndex: 1, dm: true });
    expect(items().indexOf(active()[0])).toBe(1);
    expect(items()[0].textContent).not.toContain("Active turn");
  });

  it("marks nothing when there is no active entry", async () => {
    await render({ entries: dmEntries, round: 1, activeIndex: null, dm: true });
    expect(active()).toHaveLength(0);
    expect(document.body.textContent).not.toContain("Active turn");
  });

  it("shows a hidden entry to the DM with its label and a hidden badge", async () => {
    await render({ entries: dmEntries, round: 1, activeIndex: 0, dm: true });
    const hidden = items()[1];
    expect(hidden.textContent).toContain(SECRET);
    expect(hidden.querySelector(".badge").textContent).toBe("hidden");
    expect(document.querySelectorAll(".badge")).toHaveLength(1);
  });

  it("shows a hidden entry to a player as ??? with no badge", async () => {
    await render({
      entries: playerEntries,
      round: 1,
      activeIndex: 0,
      dm: false,
    });
    expect(names()).toEqual(["Aria", "???", "Goblin"]);
    expect(document.querySelector(".badge")).toBeNull();
  });

  it("keeps the secret label out of the player DOM even when handed the DM view", async () => {
    await render({ entries: dmEntries, round: 1, activeIndex: 1, dm: false });
    expect(names()).toEqual(["Aria", "???", "Goblin"]);
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.body.textContent).not.toContain("hidden");
  });

  it("renders nothing for an empty order", async () => {
    await render({ entries: [], round: 1, activeIndex: null, dm: false });
    expect(document.querySelector("section")).toBeNull();
  });
});
