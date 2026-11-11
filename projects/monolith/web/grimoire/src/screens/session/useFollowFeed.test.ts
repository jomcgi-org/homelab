import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFollowFeed } from "./useFollowFeed";

/** A page `height` tall, scrolled so `fromBottom` pixels lie below the fold. */
function page(height: number, fromBottom: number) {
  vi.spyOn(document.documentElement, "scrollHeight", "get").mockReturnValue(
    height,
  );
  vi.stubGlobal("innerHeight", 800);
  vi.stubGlobal("scrollY", height - 800 - fromBottom);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useFollowFeed", () => {
  it("keeps following when a tall entry arrives for a reader at the bottom", () => {
    const scrollTo = vi.fn();
    vi.stubGlobal("scrollTo", scrollTo);
    vi.stubGlobal("requestAnimationFrame", (run: () => void) => run());
    page(2000, 0);
    const feed = renderHook(({ count }) => useFollowFeed(count, true), {
      initialProps: { count: 3 },
    });
    act(() => void window.dispatchEvent(new Event("scroll")));
    scrollTo.mockClear();

    // The new entry adds far more than the near-bottom allowance.
    page(3000, 1000);
    feed.rerender({ count: 4 });
    expect(scrollTo).toHaveBeenCalledWith({ top: 3000 });
    expect(feed.result.current.behind).toBe(0);
  });

  it("leaves a reader who scrolled up in place and counts what arrived", () => {
    const scrollTo = vi.fn();
    vi.stubGlobal("scrollTo", scrollTo);
    vi.stubGlobal("requestAnimationFrame", (run: () => void) => run());
    page(2000, 0);
    const feed = renderHook(({ count }) => useFollowFeed(count, true), {
      initialProps: { count: 3 },
    });
    page(2000, 900);
    act(() => void window.dispatchEvent(new Event("scroll")));
    scrollTo.mockClear();

    feed.rerender({ count: 5 });
    expect(scrollTo).not.toHaveBeenCalled();
    expect(feed.result.current.behind).toBe(2);

    // Scrolling back down by hand clears the notice.
    page(2100, 0);
    act(() => void window.dispatchEvent(new Event("scroll")));
    expect(feed.result.current.behind).toBe(0);
  });
});
