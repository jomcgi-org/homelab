import { describe, expect, test, vi } from "vitest";

import { setupVisualViewport } from "./visual-viewport.js";

function createEventSource(properties = {}) {
  const listeners = new Map();

  return {
    ...properties,
    addEventListener: vi.fn((type, listener) => {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    }),
    removeEventListener: vi.fn((type, listener) => {
      listeners.get(type)?.delete(listener);
    }),
    dispatch(type, event = {}) {
      for (const listener of listeners.get(type) ?? []) listener(event);
    },
    listenerCount(type) {
      return listeners.get(type)?.size ?? 0;
    },
  };
}

function createFixture({
  matches = true,
  height = 720,
  clientHeight = 800,
} = {}) {
  const values = new Map();
  const style = {
    setProperty: vi.fn((name, value) => values.set(name, value)),
    removeProperty: vi.fn((name) => values.delete(name)),
    getPropertyValue: vi.fn((name) => values.get(name) ?? ""),
  };
  const element = { clientHeight, style };
  const visualViewport = createEventSource({ height, offsetTop: 0 });
  const mediaQuery = createEventSource({ matches });
  const window = {
    visualViewport,
    matchMedia: vi.fn(() => mediaQuery),
    scrollTo: vi.fn(),
  };
  const measure = vi.fn();
  const apply = vi.fn();

  return {
    apply,
    element,
    measure,
    mediaQuery,
    style,
    values,
    visualViewport,
    window,
  };
}

describe("setupVisualViewport", () => {
  test("writes the viewport height on subscribe and resize", () => {
    const fixture = createFixture();

    setupVisualViewport(fixture.window, fixture.element, {
      measure: fixture.measure,
      apply: fixture.apply,
    });

    expect(fixture.values.get("--console-h")).toBe("720px");

    fixture.visualViewport.height = 414;
    fixture.visualViewport.dispatch("resize");

    expect(fixture.values.get("--console-h")).toBe("414px");
    expect(fixture.style.setProperty).toHaveBeenCalledTimes(2);
  });

  test("is a no-op when visualViewport is absent", () => {
    const matchMedia = vi.fn();
    const element = {
      style: {
        setProperty: vi.fn(),
        removeProperty: vi.fn(),
      },
    };

    const teardown = setupVisualViewport({ matchMedia }, element, {
      measure: vi.fn(),
      apply: vi.fn(),
    });

    expect(() => teardown()).not.toThrow();
    expect(matchMedia).not.toHaveBeenCalled();
    expect(element.style.setProperty).not.toHaveBeenCalled();
    expect(element.style.removeProperty).not.toHaveBeenCalled();
  });

  test("does not write or subscribe to viewport events when media does not match", () => {
    const fixture = createFixture({ matches: false });

    setupVisualViewport(fixture.window, fixture.element, {
      measure: fixture.measure,
      apply: fixture.apply,
    });

    expect(fixture.style.setProperty).not.toHaveBeenCalled();
    expect(fixture.visualViewport.addEventListener).not.toHaveBeenCalled();
    expect(fixture.mediaQuery.listenerCount("change")).toBe(1);
  });

  test("starts and stops viewport handling as the media query changes", () => {
    const fixture = createFixture({ matches: false });

    setupVisualViewport(fixture.window, fixture.element, {
      measure: fixture.measure,
      apply: fixture.apply,
    });

    fixture.mediaQuery.matches = true;
    fixture.mediaQuery.dispatch("change", { matches: true });

    expect(fixture.values.get("--console-h")).toBe("720px");
    expect(fixture.visualViewport.listenerCount("resize")).toBe(1);
    expect(fixture.visualViewport.listenerCount("scroll")).toBe(1);

    fixture.mediaQuery.matches = false;
    fixture.mediaQuery.dispatch("change", { matches: false });

    expect(fixture.values.has("--console-h")).toBe(false);
    expect(fixture.style.removeProperty).toHaveBeenCalledWith("--console-h");
    expect(fixture.visualViewport.listenerCount("resize")).toBe(0);
    expect(fixture.visualViewport.listenerCount("scroll")).toBe(0);
  });

  test("teardown removes every listener it added", () => {
    const fixture = createFixture();
    const teardown = setupVisualViewport(fixture.window, fixture.element, {
      measure: fixture.measure,
      apply: fixture.apply,
    });

    teardown();

    expect(fixture.visualViewport.listenerCount("resize")).toBe(0);
    expect(fixture.visualViewport.listenerCount("scroll")).toBe(0);
    expect(fixture.mediaQuery.listenerCount("change")).toBe(0);
    expect(fixture.visualViewport.removeEventListener).toHaveBeenCalledWith(
      "resize",
      expect.any(Function),
    );
    expect(fixture.visualViewport.removeEventListener).toHaveBeenCalledWith(
      "scroll",
      expect.any(Function),
    );
    expect(fixture.mediaQuery.removeEventListener).toHaveBeenCalledWith(
      "change",
      expect.any(Function),
    );
  });

  test("scrolls the layout viewport to the top on resize if offset is non-zero", () => {
    const fixture = createFixture();

    setupVisualViewport(fixture.window, fixture.element, {
      measure: fixture.measure,
      apply: fixture.apply,
    });
    expect(fixture.window.scrollTo).not.toHaveBeenCalled();

    fixture.visualViewport.offsetTop = 32;
    fixture.visualViewport.dispatch("resize");
    expect(fixture.window.scrollTo).toHaveBeenCalledOnce();
    expect(fixture.window.scrollTo).toHaveBeenCalledWith(0, 0);
  });

  test("does not scroll the layout viewport on scroll events", () => {
    const fixture = createFixture();

    setupVisualViewport(fixture.window, fixture.element, {
      measure: fixture.measure,
      apply: fixture.apply,
    });
    expect(fixture.window.scrollTo).not.toHaveBeenCalled();

    fixture.visualViewport.offsetTop = 32;
    fixture.visualViewport.dispatch("scroll");
    expect(fixture.window.scrollTo).not.toHaveBeenCalled();
  });

  test("measures before writing height and applies the captured value after", () => {
    const fixture = createFixture();
    const order = [];
    fixture.style.setProperty.mockImplementation((name, value) => {
      fixture.values.set(name, value);
      order.push(`write ${value}`);
    });
    const measure = vi.fn(() => {
      order.push("measure");
      return true;
    });
    const apply = vi.fn((measured) => order.push(`apply ${measured}`));

    setupVisualViewport(fixture.window, fixture.element, { measure, apply });

    expect(order).toEqual(["measure", "write 720px", "apply true"]);

    order.length = 0;
    fixture.visualViewport.height = 390;
    fixture.visualViewport.dispatch("resize");
    expect(order).toEqual(["measure", "write 390px", "apply true"]);
    expect(apply).toHaveBeenLastCalledWith(true);
  });

  test("does not re-pin on mount when the fallback already has the viewport height", () => {
    const fixture = createFixture({ height: 720, clientHeight: 720 });

    setupVisualViewport(fixture.window, fixture.element, {
      measure: fixture.measure,
      apply: fixture.apply,
    });

    expect(fixture.values.get("--console-h")).toBe("720px");
    expect(fixture.measure).not.toHaveBeenCalled();
    expect(fixture.apply).not.toHaveBeenCalled();

    fixture.visualViewport.dispatch("resize");
    expect(fixture.measure).toHaveBeenCalledOnce();
    expect(fixture.apply).toHaveBeenCalledOnce();
  });

  test("uses the supplied mobile media query", () => {
    const fixture = createFixture();

    setupVisualViewport(
      fixture.window,
      fixture.element,
      { measure: fixture.measure, apply: fixture.apply },
      "(max-width: 640px)",
    );

    expect(fixture.window.matchMedia).toHaveBeenCalledWith(
      "(max-width: 640px)",
    );
  });
});
