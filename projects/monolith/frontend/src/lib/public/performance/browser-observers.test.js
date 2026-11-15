import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installPerformanceObservers } from "./browser-observers.js";

let intersection;
let mutation;
let target;

beforeEach(() => {
  vi.useFakeTimers({
    toFake: [
      "setTimeout",
      "clearTimeout",
      "setInterval",
      "clearInterval",
      "performance",
    ],
  });
  target = {
    isConnected: true,
    matches: () => true,
    checkVisibility: vi.fn(() => true),
  };
  vi.stubGlobal("window", {});
  vi.stubGlobal("document", {
    querySelector: () => target,
    querySelectorAll: () => [target],
  });
  vi.stubGlobal(
    "PerformanceObserver",
    class {
      static supportedEntryTypes = [];
    },
  );
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(callback, options) {
        this.callback = callback;
        this.options = options;
        intersection = this;
      }
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    },
  );
  vi.stubGlobal(
    "MutationObserver",
    class {
      constructor(callback) {
        this.callback = callback;
        mutation = this;
      }
      observe = vi.fn();
      disconnect = vi.fn();
    },
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function intersect(visible) {
  intersection.callback([
    {
      target,
      isIntersecting: visible,
      intersectionRatio: visible ? 1 : 0,
      intersectionRect: { width: visible ? 100 : 0, height: visible ? 30 : 0 },
    },
  ]);
}

describe("visible content observation", () => {
  it("requests a positive threshold for edge-adjacent zero-area intersections", () => {
    installPerformanceObservers({
      contentSelector: ".data",
      contentTimeoutMs: 1000,
    });
    expect(intersection.options.threshold).toEqual([0, Number.EPSILON]);
    intersection.callback([
      {
        target,
        isIntersecting: true,
        intersectionRatio: 0,
        intersectionRect: { width: 100, height: 0 },
      },
    ]);
    vi.advanceTimersByTime(100);
    expect(window.__publicAppPerf.visibleContentMs).toBeNull();
    intersect(true);
    expect(window.__publicAppPerf.visibleContentMs).toBe(100);
  });
  it("does not count a match whose ancestor clips its entire intersection", () => {
    installPerformanceObservers({
      contentSelector: ".data",
      contentTimeoutMs: 1000,
    });
    intersect(false);
    vi.advanceTimersByTime(900);
    expect(window.__publicAppPerf.visibleContentMs).toBeNull();
    expect(target.checkVisibility).not.toHaveBeenCalled();
    window.__stopPublicAppContentObservation();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("detects CSS-only opacity changes without mutation, load or resize events", () => {
    target.checkVisibility.mockReturnValue(false);
    installPerformanceObservers({
      contentSelector: ".data",
      contentTimeoutMs: 1000,
    });
    intersect(true);
    vi.advanceTimersByTime(200);
    expect(window.__publicAppPerf.visibleContentMs).toBeNull();
    target.checkVisibility.mockReturnValue(true);
    vi.advanceTimersByTime(100);
    expect(window.__publicAppPerf.visibleContentMs).toBe(300);
    expect(window.__publicAppPerf.contentObservationStopReason).toBe("matched");
    expect(intersection.disconnect).toHaveBeenCalled();
    expect(mutation.disconnect).toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("detects a CSS-only position change through the intersection observer", () => {
    installPerformanceObservers({
      contentSelector: ".data",
      contentTimeoutMs: 1000,
    });
    intersect(false);
    vi.advanceTimersByTime(250);
    intersect(true);
    expect(window.__publicAppPerf.visibleContentMs).toBe(250);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ends at a fixed deadline and cannot turn a later change into a load milestone", () => {
    installPerformanceObservers({
      contentSelector: ".data",
      contentTimeoutMs: 250,
    });
    intersect(false);
    vi.advanceTimersByTime(300);
    intersect(true);
    expect(window.__publicAppPerf.visibleContentMs).toBeNull();
    expect(window.__publicAppPerf.contentObservationStopReason).toBe("timeout");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("stops at the load cutoff before scripted interactions", () => {
    installPerformanceObservers({
      contentSelector: ".data",
      contentTimeoutMs: 1000,
    });
    intersect(false);
    window.__stopPublicAppContentObservation();
    intersect(true);
    expect(window.__publicAppPerf.visibleContentMs).toBeNull();
    expect(window.__publicAppPerf.contentObservationStopReason).toBe("cutoff");
    expect(vi.getTimerCount()).toBe(0);
  });
});
