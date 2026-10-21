// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import SchemeToggle from "./SchemeToggle.svelte";

let instance;
let target;

afterEach(() => {
  if (instance) unmount(instance);
  instance = undefined;
  target?.remove();
  delete document.startViewTransition;
  delete document.documentElement.dataset.theme;
  vi.restoreAllMocks();
});

function setup(theme) {
  document.documentElement.dataset.theme = theme;
  target = document.createElement("div");
  document.body.append(target);
  instance = mount(SchemeToggle, { target });
  flushSync();
  return target.querySelector("button.scheme");
}

describe("SchemeToggle", () => {
  it("flips through a view transition where the browser has one", () => {
    const pending = [];
    document.startViewTransition = vi.fn((update) => pending.push(update));
    const button = setup("light");
    button.click();
    expect(document.documentElement.dataset.theme).toBe("light");
    pending.shift()();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("counts a second click made before the first transition runs", () => {
    const pending = [];
    document.startViewTransition = vi.fn((update) => pending.push(update));
    const button = setup("dark");
    button.click();
    button.click();
    // A newer transition skips an older one, but both updates still run.
    while (pending.length) pending.shift()();
    flushSync();
    expect(document.documentElement.dataset.theme).toBe("dark");
    button.click();
    pending.shift()();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("switches at once without view transitions", () => {
    const button = setup("light");
    button.click();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
