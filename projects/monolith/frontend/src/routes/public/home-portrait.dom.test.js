// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { createPortrait } from "./home-portrait.js";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("createPortrait", () => {
  it("is a no-op without a 2d context", () => {
    const canvas = document.createElement("canvas");
    canvas.getContext = () => null;
    const host = document.createElement("div");
    const add = vi.spyOn(window, "addEventListener");
    const portrait = createPortrait({
      canvas,
      host,
      src: "/joe-grey.png",
      scheme: () => "light",
    });
    expect(add).not.toHaveBeenCalled();
    expect(() => portrait.destroy()).not.toThrow();
  });

  it("wires the window listeners and removes every one on destroy, without drawing a failed image", () => {
    vi.useFakeTimers();
    const ctx = {
      setTransform: vi.fn(),
      clearRect: vi.fn(),
      drawImage: vi.fn(),
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      rect: vi.fn(),
      clip: vi.fn(),
      fillRect: vi.fn(),
    };
    const canvas = document.createElement("canvas");
    canvas.getContext = () => ctx;
    const host = document.createElement("div");
    host.getBoundingClientRect = () => ({
      left: 0,
      top: 0,
      width: 300,
      height: 400,
    });
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const portrait = createPortrait({
      canvas,
      host,
      src: "/missing.png",
      scheme: () => "light",
    });
    const wired = add.mock.calls.map(([name]) => name);
    expect(wired).toEqual(
      expect.arrayContaining([
        "pointermove",
        "pointerup",
        "pointercancel",
        "scroll",
        "resize",
      ]),
    );
    // The image never loads here, so frames draw nothing rather than throwing.
    vi.advanceTimersByTime(200);
    expect(ctx.drawImage).not.toHaveBeenCalled();
    portrait.destroy();
    const removed = remove.mock.calls.map(([name]) => name);
    for (const name of wired) expect(removed).toContain(name);
    expect(vi.getTimerCount()).toBe(0);
  });
});
