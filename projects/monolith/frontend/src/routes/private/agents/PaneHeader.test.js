// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import PaneHeader from "./PaneHeader.svelte";
import { RUN_LEXICON as P } from "./run-lexicon.js";

const mounted = [];

async function render(props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(PaneHeader, {
    target,
    props: {
      sessionRow: true,
      sessionId: "local-session-1",
      onDestroy: vi.fn(),
      ...props,
    },
  });
  mounted.push({ component, target });
  await tick();
  return target;
}

async function click(element) {
  element.click();
  await tick();
}

async function keydown(key) {
  const event = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
  });
  window.dispatchEvent(event);
  await tick();
  await Promise.resolve();
  return event;
}

function menuButton(target) {
  return target.querySelector(`[aria-label="${P.labels.sessionMenu}"]`);
}

afterEach(async () => {
  vi.restoreAllMocks();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("session header menu", () => {
  test("requires two destroy clicks", async () => {
    const onDestroy = vi.fn();
    const target = await render({ onDestroy });

    expect(target.textContent).not.toContain(P.labels.headerDestroySession);
    await click(menuButton(target));
    const destroy = [...target.querySelectorAll('[role="menuitem"]')].find(
      (item) => item.textContent === P.labels.headerDestroySession,
    );
    await click(destroy);

    expect(onDestroy).not.toHaveBeenCalled();
    expect(destroy.textContent).toBe(P.labels.destroyConfirmMenu);

    await click(destroy);
    expect(onDestroy).toHaveBeenCalledTimes(1);
  });

  test("focuses the first enabled item when opened", async () => {
    const target = await render({ selectedRun: true });

    await click(menuButton(target));

    const items = [...target.querySelectorAll('[role="menuitem"]')];
    expect(document.activeElement.textContent).toBe(P.labels.headerBackToRun);
    expect(document.activeElement.tabIndex).toBe(0);
    expect(items.filter((item) => item.tabIndex === 0)).toHaveLength(1);
  });

  test("ArrowDown wraps from the last enabled item", async () => {
    const target = await render();
    await click(menuButton(target));

    await keydown("End");
    expect(document.activeElement.textContent).toBe(
      P.labels.headerDestroySession,
    );

    await keydown("ArrowDown");
    expect(document.activeElement.textContent).toBe(P.labels.headerCopyId);
  });

  test("Home and End jump to the first and last enabled items", async () => {
    const target = await render();
    await click(menuButton(target));

    await keydown("End");
    expect(document.activeElement.textContent).toBe(
      P.labels.headerDestroySession,
    );

    await keydown("Home");
    expect(document.activeElement.textContent).toBe(P.labels.headerCopyId);
  });

  test("Tab closes the menu without preventing focus navigation", async () => {
    const target = await render();
    await click(menuButton(target));

    const event = await keydown("Tab");

    expect(event.defaultPrevented).toBe(false);
    expect(target.querySelector('[role="menu"]')).toBe(null);
  });

  test("Copy id disarms Destroy", async () => {
    const onDestroy = vi.fn();
    const target = await render({ onDestroy });
    await click(menuButton(target));
    const destroy = [...target.querySelectorAll('[role="menuitem"]')].find(
      (item) => item.textContent === P.labels.headerDestroySession,
    );
    await click(destroy);

    const copy = [...target.querySelectorAll('[role="menuitem"]')].find(
      (item) => item.textContent === P.labels.headerCopyId,
    );
    await click(copy);

    expect(destroy.textContent).toBe(P.labels.headerDestroySession);
    await click(destroy);
    expect(onDestroy).not.toHaveBeenCalled();
  });

  test("Escape closes the menu and returns focus", async () => {
    const target = await render();
    const trigger = menuButton(target);
    await click(trigger);

    await keydown("Escape");

    expect(target.querySelector('[role="menu"]')).toBe(null);
    expect(document.activeElement).toBe(trigger);
  });

  test("an outside click closes, keeps focus where it went, and resets confirmation", async () => {
    const target = await render();
    const trigger = menuButton(target);
    await click(trigger);
    const destroy = [...target.querySelectorAll('[role="menuitem"]')].find(
      (item) => item.textContent === P.labels.headerDestroySession,
    );
    await click(destroy);

    const outside = document.createElement("button");
    document.body.append(outside);
    outside.focus();
    await click(outside);

    expect(target.querySelector('[role="menu"]')).toBe(null);
    expect(document.activeElement).toBe(outside);

    await click(trigger);
    expect(target.textContent).toContain(P.labels.headerDestroySession);
    outside.remove();
  });

  test("only renders Back to run with a selected run", async () => {
    const standalone = await render({ selectedRun: false });
    await click(menuButton(standalone));
    expect(standalone.textContent).not.toContain(P.labels.headerBackToRun);

    const inRun = await render({ selectedRun: true });
    await click(menuButton(inRun));
    expect(inRun.textContent).toContain(P.labels.headerBackToRun);
  });
});
