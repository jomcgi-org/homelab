// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../../test/SheetRoute.svelte";
import { page, setPage } from "../../../../../test/profile-page.svelte.js";

vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));

let instance;
let target;
let request;
const frame = () => target.querySelector("iframe");
const source = () => frame().getAttribute("src");

beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.disableIframePageLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  vi.useFakeTimers();
  request = vi
    .fn()
    .mockResolvedValue({ ok: true, json: async () => ({ version: "first" }) });
  vi.stubGlobal("fetch", request);
  target = document.createElement("div");
  document.body.append(target);
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function render(path = "/artifact/fixture", id = "fixture") {
  page.params = { id };
  setPage(`https://jomcgi.dev${path}`);
  instance = mount(Route, { target, props: { surface: "artifact" } });
  await tick();
}

async function poll(version) {
  request.mockResolvedValueOnce({ ok: true, json: async () => ({ version }) });
  await vi.advanceTimersByTimeAsync(3000);
  await tick();
}

describe("artifact in the production root and public layouts", () => {
  it.each([
    "/artifact/fixture",
    "/public/artifact/fixture",
    "/private/artifact/fixture",
  ])(
    "keeps %s isolated and without site navigation or an inset content frame",
    async (path) => {
      await render(path);
      expect(
        target.querySelector("main.td.sheet.artifact-sheet"),
      ).not.toBeNull();
      expect(target.querySelectorAll("iframe")).toHaveLength(1);
      expect(frame().getAttribute("sandbox")).toBe("allow-scripts");
      expect(frame().getAttribute("title")).toBe("artifact");
      expect(source()).toBe("/artifact/fixture/raw");
      expect(document.title).toBe("Artifact");
      expect(
        target.querySelector(".md-nav, .chrome, .spine, .frame, .scheme"),
      ).toBeNull();
      expect(frame().hasAttribute("srcdoc")).toBe(false);
    },
  );

  it("waits 3000ms, records a baseline and reloads only a changed version with encoded cache busting", async () => {
    await render();
    await vi.advanceTimersByTimeAsync(2999);
    expect(request).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(request).toHaveBeenCalledWith("/artifact/fixture/version");
    expect(source()).toBe("/artifact/fixture/raw");
    await poll("first");
    expect(source()).toBe("/artifact/fixture/raw");
    await poll("second /+?&=雪");
    const updated = `/artifact/fixture/raw?v=${encodeURIComponent("second /+?&=雪")}`;
    expect(source()).toBe(updated);
    await poll("second /+?&=雪");
    expect(source()).toBe(updated);
    expect(frame().getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("tolerates HTTP, network, JSON and missing-version failures without losing the baseline", async () => {
    await render();
    await poll("first");
    const failures = [
      () => Promise.resolve({ ok: false }),
      () => Promise.reject(new Error("offline")),
      () =>
        Promise.resolve({
          ok: true,
          json: async () => {
            throw new SyntaxError("bad JSON");
          },
        }),
      ...[null, {}, { version: "" }, { version: 0 }].map(
        (data) => () => Promise.resolve({ ok: true, json: async () => data }),
      ),
    ];
    for (const fail of failures) {
      request.mockImplementationOnce(fail);
      await vi.advanceTimersByTimeAsync(3000);
      await tick();
      expect(source()).toBe("/artifact/fixture/raw");
    }
    await poll("recovered");
    expect(source()).toBe("/artifact/fixture/raw?v=recovered");
  });

  it("resets the raw URL on a route id change and keeps the existing polling baseline", async () => {
    await render();
    await poll("first");
    await poll("second");
    page.params = { id: "another" };
    setPage("https://jomcgi.dev/artifact/another");
    await tick();
    expect(source()).toBe("/artifact/another/raw");
    await poll("second");
    expect(source()).toBe("/artifact/another/raw");
    await poll("third");
    expect(request).toHaveBeenLastCalledWith("/artifact/another/version");
    expect(source()).toBe("/artifact/another/raw?v=third");
  });

  it("clears the interval on unmount and makes no subsequent requests", async () => {
    const clear = vi.spyOn(globalThis, "clearInterval");
    await render();
    await poll("first");
    await unmount(instance);
    instance = undefined;
    const calls = request.mock.calls.length;
    expect(clear).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(9000);
    expect(request).toHaveBeenCalledTimes(calls);
    expect(vi.getTimerCount()).toBe(0);
    expect(document.head.textContent).not.toContain("overflow: hidden");
  });
});
