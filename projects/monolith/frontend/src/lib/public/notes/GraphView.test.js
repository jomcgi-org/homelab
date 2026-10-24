// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import GraphView from "./GraphView.svelte";
import { setCachedGraph } from "./graph-cache.js";
vi.mock("$lib/components/notes/KnowledgeGraph.svelte", () => ({
  default: () => {},
}));
vi.mock("$lib/components/notes/NotePanel.svelte", () => ({
  default: () => {},
}));
vi.mock("$lib/components/notes/GraphLegend.svelte", () => ({
  default: () => {},
}));
vi.mock("$lib/components/notes/GraphSearch.svelte", () => ({
  default: () => {},
}));
let component;
let target;
const START = Date.parse("2026-10-24T12:00:00Z");
function graphResponse() {
  return new Response(
    JSON.stringify({ nodes: [{ id: "current", type: "fact" }], edges: [] }),
    {
      headers: {
        "cache-control": "public, max-age=2, s-maxage=2, must-revalidate",
        date: new Date(START).toUTCString(),
      },
    },
  );
}
async function open() {
  component = mount(GraphView, { target });
  flushSync();
  await vi.advanceTimersByTimeAsync(0);
  flushSync();
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(START);
  setCachedGraph(null, new Headers());
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  target = document.createElement("div");
  document.body.append(target);
});
afterEach(() => {
  if (component) unmount(component);
  component = null;
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
describe("graph lifecycle freshness", () => {
  it("reuses a fresh remount without renewing its expiry, and clears on failed refresh", async () => {
    const fetcher = vi
      .fn()
      .mockImplementationOnce(async () => graphResponse())
      .mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetcher);
    await open();
    expect(target.textContent).toContain("1 NODES");
    await vi.advanceTimersByTimeAsync(1000);
    unmount(component);
    component = null;
    await open();
    expect(fetcher).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    flushSync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(target.textContent).toContain("0 NODES");
    expect(target.textContent).toContain("unavailable");
  });
  it("defers hidden refresh and fetches when visible without displaying expired nodes", async () => {
    const fetcher = vi
      .fn()
      .mockImplementationOnce(async () => graphResponse())
      .mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetcher);
    await open();
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await vi.advanceTimersByTimeAsync(2000);
    flushSync();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(target.textContent).toContain("0 NODES");
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(target.textContent).toContain("unavailable");
  });
  it("cancels the expiry timer when unmounted", async () => {
    const fetcher = vi.fn(async () => graphResponse());
    vi.stubGlobal("fetch", fetcher);
    await open();
    unmount(component);
    component = null;
    await vi.advanceTimersByTimeAsync(10000);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
