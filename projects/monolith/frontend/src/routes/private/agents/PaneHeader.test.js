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

  test("Escape closes the menu and returns focus", async () => {
    const target = await render();
    const trigger = menuButton(target);
    await click(trigger);

    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    await tick();

    expect(target.querySelector('[role="menu"]')).toBe(null);
    expect(document.activeElement).toBe(trigger);
  });

  test("an outside click closes, returns focus, and resets confirmation", async () => {
    const target = await render();
    const trigger = menuButton(target);
    await click(trigger);
    const destroy = [...target.querySelectorAll('[role="menuitem"]')].find(
      (item) => item.textContent === P.labels.headerDestroySession,
    );
    await click(destroy);

    const outside = document.createElement("button");
    document.body.append(outside);
    await click(outside);

    expect(target.querySelector('[role="menu"]')).toBe(null);
    expect(document.activeElement).toBe(trigger);

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
