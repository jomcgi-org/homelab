// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, unmount, tick } from "svelte";
import Page from "./+page.svelte";

vi.mock("$app/navigation", () => ({ goto: vi.fn() }));
// The map's WebGL implementation is outside gallery behavior. Keep the actual
// view toggle and parent lifecycle, with an inert component in the map slot.
vi.mock("$lib/public/components/trips/TripMap.svelte", async () => ({
  default: (await import("$lib/public/components/trips/PhotoGrid.svelte"))
    .default,
}));

let instance;
let props;
const settle = async () => {
  for (let i = 0; i < 5; i++) await tick();
};
const dialog = () => document.querySelector('[role="dialog"]');
const button = (label) =>
  document.querySelector(`button[aria-label="${label}"]`);
const textButton = (text) =>
  [...document.querySelectorAll("button")].find(
    (item) => item.textContent.trim() === text,
  );
const photos = () =>
  ["a", "b", "c"].map((id, i) => ({
    id,
    image: `${id}.jpg`,
    tags: i === 1 ? ["town"] : ["trail"],
    taken_at: "2026-10-17T12:00:00Z",
    imgGallery: `https://images.example/${id}?signed=gallery`,
    imgDisplay: `https://images.example/${id}?signed=display`,
  }));

async function render(points = photos()) {
  // Getter props let the test exercise SvelteKit-style data replacement without
  // mounting a new page instance (the stale selection regression).
  const { writable } = await import("svelte/store");
  const { fromStore } = await import("svelte/store");
  const data = writable({
    trip: { slug: "walk", tz: "America/Vancouver" },
    points,
  });
  const reactive = fromStore(data);
  props = data;
  instance = mount(Page, {
    target: document.body,
    props: {
      get data() {
        return reactive.current;
      },
    },
  });
  await settle();
}

async function open(index) {
  const tile = button(`Open photo ${index + 1}`);
  tile.focus();
  tile.click();
  await settle();
  return tile;
}

async function key(key, shiftKey = false) {
  document.activeElement.dispatchEvent(
    new KeyboardEvent("keydown", {
      key,
      shiftKey,
      bubbles: true,
      cancelable: true,
    }),
  );
  await settle();
}

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  await settle();
  document.body.innerHTML = "";
});

describe("timeline viewer", () => {
  it.each([0, 2])(
    "opens clicked index %s with its signed URL and timezone",
    async (index) => {
      await render();
      await open(index);
      expect(dialog().querySelector("img").src).toBe(
        photos()[index].imgDisplay,
      );
      expect(dialog().textContent).toContain(`${index + 1} / 3`);
      expect(dialog().textContent).toContain(
        new Date(photos()[index].taken_at).toLocaleString("en-CA", {
          timeZone: "America/Vancouver",
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        }),
      );
      expect(document.activeElement).toBe(button("Close"));
    },
  );

  it("retains native Enter activation for photo zero", async () => {
    await render();
    const tile = button("Open photo 1");
    expect(tile.tagName).toBe("BUTTON");
    expect(tile.disabled).toBe(false);
    tile.focus();
    await key("Enter");
    // happy-dom has no keyboard default actions. Model the native button click
    // emitted by Enter; deployed browser acceptance remains on #7051.
    tile.click();
    await settle();
    expect(dialog().querySelector("img").src).toBe(photos()[0].imgDisplay);
  });

  it("bounds buttons and arrows to the displayed filtered collection", async () => {
    await render();
    textButton("trail").click();
    await settle();
    await open(0);
    expect(button("Previous photo").disabled).toBe(true);
    await key("ArrowLeft");
    expect(dialog().querySelector("img").src).toBe(photos()[0].imgDisplay);
    await key("ArrowRight");
    button("Previous photo").click();
    await settle();
    expect(dialog().querySelector("img").src).toBe(photos()[0].imgDisplay);
    button("Next photo").click();
    await settle();
    expect(dialog().querySelector("img").src).toBe(photos()[2].imgDisplay);
    expect(dialog().textContent).toContain("2 / 2");
    expect(button("Next photo").disabled).toBe(true);
    await key("ArrowRight");
    expect(dialog().querySelector("img").src).toBe(photos()[2].imgDisplay);
    await key("ArrowLeft");
    expect(dialog().querySelector("img").src).toBe(photos()[0].imgDisplay);
  });

  it.each(["Escape", "Close", "Close photo viewer"])(
    "dismisses with %s and restores focus",
    async (method) => {
      await render();
      const tile = await open(1);
      if (method === "Escape") await key(method);
      else {
        button(method).click();
        await settle();
      }
      expect(dialog()).toBeNull();
      expect(document.activeElement).toBe(tile);
      await key("ArrowRight");
      expect(dialog()).toBeNull();
    },
  );

  it("contains tab and programmatic focus inside the dialog", async () => {
    await render();
    await open(0);
    button("Next photo").focus();
    await key("Tab");
    expect(document.activeElement).toBe(button("Close photo viewer"));
    await key("Tab", true);
    expect(document.activeElement).toBe(button("Next photo"));
    textButton("trail").focus();
    expect(document.activeElement).toBe(button("Close"));
  });

  it("dismisses on filtering and restores to the header when the tile disappears", async () => {
    await render();
    await open(1);
    textButton("trail").click();
    await settle();
    expect(dialog()).toBeNull();
    expect(document.activeElement).toBe(document.querySelector("header.head"));
    await open(1);
    expect(dialog().querySelector("img").src).toBe(photos()[2].imgDisplay);
  });

  it("dismisses on map switching and does not reopen on photos", async () => {
    await render();
    await open(0);
    textButton("Map").click();
    await settle();
    expect(dialog()).toBeNull();
    expect(document.activeElement).toBe(document.querySelector("header.head"));
    textButton("Photos").click();
    await settle();
    expect(dialog()).toBeNull();
    expect(button("Open photo 1")).not.toBeNull();
  });

  it("dismisses on refreshed data and handles empty filtered results", async () => {
    await render();
    textButton("trail").click();
    await settle();
    await open(0);
    props.set({ trip: { slug: "walk", tz: "UTC" }, points: [photos()[1]] });
    await settle();
    expect(dialog()).toBeNull();
    expect(button("Open photo 1")).toBeNull();
    expect(document.body.textContent).toContain("0 / 1 photos");
    expect(document.activeElement).toBe(document.querySelector("header.head"));
  });

  it("handles an empty photo collection", async () => {
    await render([]);
    expect(dialog()).toBeNull();
    expect(button("Open photo 1")).toBeNull();
    expect(document.body.textContent).toContain("0 / 0 photos");
  });

  it("bounds a single photo and dismisses even when refreshed data retains its id", async () => {
    await render([photos()[0]]);
    await open(0);
    expect(button("Previous photo").disabled).toBe(true);
    expect(button("Next photo").disabled).toBe(true);
    await key("ArrowLeft");
    await key("ArrowRight");
    expect(dialog().textContent).toContain("1 / 1");
    props.set({ trip: { slug: "walk", tz: "UTC" }, points: [photos()[0]] });
    await settle();
    expect(dialog()).toBeNull();
  });
});
