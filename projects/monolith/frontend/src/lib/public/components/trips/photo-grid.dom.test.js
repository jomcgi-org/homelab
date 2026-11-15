// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { fromStore, writable } from "svelte/store";
import PhotoGrid from "./PhotoGrid.svelte";

const photos = Array.from({ length: 44 }, (_, index) => ({
  id: `photo-${index}`,
  imgGallery: `/gallery-${index}.jpg`,
}));
let instance;
let current;
let callback;
let observer;
let opened;
const settle = async () => {
  for (let i = 0; i < 4; i++) await tick();
};
const tiles = () => [...document.querySelectorAll(".tile")];
const images = () => [...document.querySelectorAll(".tile > img")];
beforeEach(() => {
  observer = {
    observe: vi.fn(),
    unobserve: vi.fn(),
    disconnect: vi.fn(),
  };
  vi.stubGlobal(
    "IntersectionObserver",
    vi.fn(function (handler) {
      callback = handler;
      return observer;
    }),
  );
  opened = vi.fn();
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render(photoList = photos) {
  current = fromStore(writable(photoList));
  instance = mount(PhotoGrid, {
    target: document.body,
    props: {
      get photos() {
        return current.current;
      },
      onOpen: opened,
    },
  });
  await settle();
}

describe("gallery image scheduling", () => {
  it("keeps first-view images eager and limits later URLs to the viewport margin", async () => {
    await render();
    expect(tiles()).toHaveLength(44);
    expect(images().map((image) => image.getAttribute("src"))).toEqual(
      photos.slice(0, 6).map((photo) => photo.imgGallery),
    );
    expect(document.querySelectorAll("img[src]")).toHaveLength(6);
    expect(
      images().every((image) => image.getAttribute("loading") === "eager"),
    ).toBe(true);
    expect(
      images().every((image) => image.getAttribute("fetchpriority") === "high"),
    ).toBe(true);
    expect(IntersectionObserver).toHaveBeenCalledOnce();
    expect(IntersectionObserver.mock.calls[0][1]).toEqual({
      rootMargin: "160px 0px",
    });
    expect(observer.observe).toHaveBeenCalledTimes(38);
    callback([{ target: tiles()[6], isIntersecting: false }]);
    await settle();
    expect(images()).toHaveLength(6);
    callback([
      { target: tiles()[6], isIntersecting: true },
      { target: tiles()[7], isIntersecting: true },
    ]);
    await settle();
    expect(images().map((image) => image.getAttribute("src"))).toEqual(
      photos.slice(0, 8).map((photo) => photo.imgGallery),
    );
    expect(images()[6].getAttribute("fetchpriority")).toBe("auto");
    expect(observer.unobserve).toHaveBeenCalledWith(tiles()[6]);
  });

  it("loads keyboard intent and opens every photo with its original index", async () => {
    await render();
    tiles()[43].focus();
    await settle();
    expect(tiles()[43].querySelector("img").getAttribute("src")).toBe(
      photos[43].imgGallery,
    );
    for (const tile of tiles()) tile.click();
    expect(opened.mock.calls.map(([index]) => index)).toEqual(
      photos.map((_, index) => index),
    );
  });

  it("reveals a nearby photo without revisiting offscreen photo URLs", async () => {
    const lastUrl = vi.fn(() => photos[43].imgGallery);
    await render(
      photos.map((photo, index) =>
        index === 43
          ? {
              id: photo.id,
              get imgGallery() {
                return lastUrl();
              },
            }
          : photo,
      ),
    );
    lastUrl.mockClear();
    callback([{ target: tiles()[6], isIntersecting: true }]);
    await settle();
    expect(images()).toHaveLength(7);
    expect(lastUrl).not.toHaveBeenCalled();
  });

  it("updates reused tiles and ignores intersections for photos removed by filters", async () => {
    await render();
    const removed = tiles()[43];
    const reused = tiles()[8];
    current.current = photos.slice(0, 10).map((photo, index) => ({
      ...photo,
      imgGallery: index === 8 ? "/replacement.jpg" : photo.imgGallery,
    }));
    await settle();
    callback([
      { target: removed, isIntersecting: true },
      { target: reused, isIntersecting: true },
    ]);
    await settle();
    expect(tiles()).toHaveLength(10);
    expect(images()).toHaveLength(7);
    expect(reused.querySelector("img").getAttribute("src")).toBe(
      "/replacement.jpg",
    );
    reused.click();
    expect(opened).toHaveBeenCalledWith(8);
    current.current = [];
    await settle();
    expect(document.body.textContent).toContain("No photos for this day.");
    expect(observer.unobserve).toHaveBeenCalledWith(removed);
  });

  it("falls back to all native-lazy images when observers are unavailable", async () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    await render();
    expect(images()).toHaveLength(44);
    expect(
      images()
        .slice(6)
        .every((image) => image.getAttribute("loading") === "lazy"),
    ).toBe(true);
  });

  it("disconnects and ignores queued observations after unmount", async () => {
    await render();
    const last = tiles()[43];
    await unmount(instance);
    instance = undefined;
    expect(observer.disconnect).toHaveBeenCalledOnce();
    callback([{ target: last, isIntersecting: true }]);
    await settle();
    expect(images()).toHaveLength(0);
  });
});
