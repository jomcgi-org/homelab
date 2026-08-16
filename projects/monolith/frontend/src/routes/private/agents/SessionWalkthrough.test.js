// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import SessionWalkthrough from "./SessionWalkthrough.svelte";
import WalkthroughNarrative from "./WalkthroughNarrative.svelte";

const mounted = [];

async function render(Component, props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Component, { target, props });
  mounted.push({ component, target });
  await tick();
  return target;
}

afterEach(async () => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("conversation disclosure", () => {
  test("stays collapsed and does not fetch until opened", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ rung: 5, steps: [], stats: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const target = await render(SessionWalkthrough, {
      sessionId: 17,
      turnSeq: 3,
      model: "luna",
    });
    const details = target.querySelector("details");

    expect(details.open).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();

    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    details.dispatchEvent(new Event("toggle"));
    await tick();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("full-page narrative", () => {
  test("loads once on mount because it renders open", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        rung: 1,
        summary: {
          status: "available",
          files_changed: 1,
          insertions: 1,
          deletions: 0,
          accounted_files: 1,
          unexplained_files: 0,
        },
        steps: [],
        stats: { total_files: 1 },
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const target = await render(WalkthroughNarrative, {
      sessionId: 17,
      turnSeq: 3,
      model: "luna",
    });

    const renderedText = () => target.textContent.replace(/\s+/g, " ").trim();
    await vi.waitFor(() => expect(renderedText()).toContain("1 file changed"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(renderedText()).toContain("leaving 0 files unexplained.");
    expect(renderedText()).not.toContain("·");
  });
});
