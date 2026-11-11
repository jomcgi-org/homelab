import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "./Route.svelte";
import { setPage } from "./page.svelte.js";
import { installNetwork, installChallenge, routeData } from "./fixtures.js";

let component;
let target;
let requests;
const paths = {
  home: "",
  library: "/library",
  world: "/world?e=fixture-person",
  entity: "/entity/fixture-person",
  adventure: "/adventure/fixture-adventure",
  book: "/book/fixture-book",
  chat: "/chat",
  share: "/chat/s/fixture-share",
};
async function settle() {
  await tick();
  await new Promise((resolve) => setTimeout(resolve, 30));
  await tick();
}
async function show(surface, state = "normal") {
  const data = routeData(state);
  requests = installNetwork(state);
  setPage(`https://jomcgi.dev/app/grimoire${paths[surface]}`, surface, data);
  component = mount(Route, { target, props: { surface, data } });
  await settle();
}
async function admit() {
  const button = [...target.querySelectorAll("button")].find(
    (el) => el.textContent === "Solve fixture challenge",
  );
  expect(button).toBeTruthy();
  button.click();
  await settle();
}
beforeEach(() => {
  target = document.createElement("div");
  document.body.append(target);
  installChallenge();
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query.includes("reduce"),
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }));
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe() {}
      disconnect() {}
      unobserve() {}
    },
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    () =>
      new Proxy(
        { measureText: () => ({ width: 80 }) },
        { get: (o, key) => o[key] ?? (() => {}) },
      ),
  );
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  target.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("production Grimoire route DOM", () => {
  for (const surface of Object.keys(paths)) {
    it(`renders ${surface} with its real layout and controls`, async () => {
      await show(surface);
      if (["world", "entity", "adventure", "chat"].includes(surface)) {
        expect(requests).toEqual([]);
        expect(target.textContent).toContain("solve to");
        await admit();
        expect(requests[0]).toEqual({
          path: "/app/grimoire/chat/session",
          method: "POST",
        });
      }
      const expected = {
        home: "Grimoire",
        library: "The Lantern Atlas",
        world: "Mira of the Lantern",
        entity: "Mira of the Lantern",
        adventure: "The Paper Tide",
        book: "Mira keeps a lantern",
        chat: "Who keeps the lantern?",
        share: "Who keeps the lantern?",
      };
      expect(target.textContent).toContain(expected[surface]);
      expect(target.querySelector("h1, h2")).toBeTruthy();
      expect(target.querySelectorAll("a[href]").length).toBeGreaterThan(0);
      if (["home", "share"].includes(surface)) expect(requests).toEqual([]);
      if (surface === "share") {
        expect(target.querySelector("textarea")).toBeNull();
        expect(target.textContent).toContain("READ-ONLY");
      }
    });
  }
  it("does not fetch or mount derived corpus after rejected admission", async () => {
    await show("world", "rejected");
    await admit();
    expect(target.textContent).toContain("Verification failed");
    expect(requests.map((r) => r.path)).toEqual(["/app/grimoire/chat/session"]);
  });
  it("keeps the locked reader ungated and never requests text", async () => {
    await show("book", "locked");
    expect(target.textContent).toContain("Not available to read here");
    expect(requests).toEqual([]);
    expect(
      target.querySelector('a[href="/app/grimoire/library"]'),
    ).toBeTruthy();
  });
  for (const surface of ["library", "world", "entity", "adventure"]) {
    it(`renders ${surface} failure in the DOM`, async () => {
      await show(surface, "error");
      if (surface !== "library") await admit();
      expect(target.textContent).toMatch(
        /could not|couldn't|unavailable|failed|try again/i,
      );
    });
  }
  it("renders an empty share without fork or input", async () => {
    await show("share", "empty");
    expect(target.querySelector("textarea")).toBeNull();
    expect(target.textContent).not.toContain("CONTINUE THIS CHAT");
  });
  it("offers fork only through the real Grimoire fork admission helper", async () => {
    await show("share");
    [...target.querySelectorAll("button")]
      .find((el) => el.textContent.includes("CONTINUE THIS CHAT"))
      .click();
    await settle();
    expect(requests).toEqual([]);
    await admit();
    expect(requests[0]).toEqual({
      path: "/app/grimoire/chat/fork",
      method: "POST",
    });
  });
});
