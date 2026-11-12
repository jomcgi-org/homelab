import { act, cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { chooseOption, openApp } from "../test/app";
import { ids } from "../test/fixtures";
import { world, worldOther } from "../test/world";

const storageKey = "grimoire.appearance";
let dark = false;
let media: MediaQueryList;
let add: ReturnType<typeof vi.spyOn>;
let remove: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  window.localStorage.clear();
  dark = false;
  const target = new EventTarget();
  Object.defineProperties(target, {
    matches: { get: () => dark },
    media: { value: "(prefers-color-scheme: dark)" },
  });
  media = target as MediaQueryList;
  add = vi.spyOn(media, "addEventListener");
  remove = vi.spyOn(media, "removeEventListener");
  const matchMedia = window.matchMedia.bind(window);
  vi.spyOn(window, "matchMedia").mockImplementation((query) =>
    query === media.media ? media : matchMedia(query),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

function changeSystem(value: boolean) {
  act(() => {
    dark = value;
    media.dispatchEvent(new Event("change"));
  });
}

function expectMode(mode: "light" | "dark") {
  expect(document.documentElement.getAttribute("data-theme")).toBe(mode);
  expect(document.documentElement.getAttribute("data-astryx-theme")).toBe(
    "sheet",
  );
}

async function select(option: string) {
  await chooseOption(document.body, "Appearance", option);
}

describe("appearance through Providers and App", () => {
  it.each([false, true])(
    "uses system on the first render (dark: %s)",
    (value) => {
      dark = value;
      openApp(world);
      expectMode(value ? "dark" : "light");
      expect(
        screen.getByRole("combobox", { name: "Appearance" }).textContent,
      ).toContain("System");
      expect(window.localStorage.getItem(storageKey)).toBeNull();
    },
  );

  it("follows OS changes in System and removes its listener and root scope on unmount", () => {
    openApp(world);
    changeSystem(true);
    expectMode("dark");
    changeSystem(false);
    expectMode("light");
    const listener = add.mock.calls.find(
      ([event]: unknown[]) => event === "change",
    )?.[1];
    cleanup();
    expect(remove).toHaveBeenCalledWith("change", listener);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(document.documentElement.hasAttribute("data-astryx-theme")).toBe(
      false,
    );
  });

  it.each(["Light", "Dark"])(
    "%s overrides the OS and persists across remounts",
    async (option) => {
      const { backend } = openApp(world);
      await select(option);
      const mode = option.toLowerCase() as "light" | "dark";
      expectMode(mode);
      changeSystem(true);
      expectMode(mode);
      changeSystem(false);
      expectMode(mode);
      expect(window.localStorage.getItem(storageKey)).toBe(mode);
      cleanup();
      backend.restore();
      dark = mode === "light";
      openApp(world);
      expectMode(mode);
      expect(
        screen.getByRole("combobox", { name: "Appearance" }).textContent,
      ).toContain(option);
    },
  );

  it("returns to the current system scheme and resumes following it", async () => {
    const { backend } = openApp(world);
    await select("Dark");
    changeSystem(true);
    await select("Light");
    expectMode("light");
    await select("System");
    expectMode("dark");
    changeSystem(false);
    expectMode("light");
    expect(window.localStorage.getItem(storageKey)).toBe("system");
    cleanup();
    backend.restore();
    dark = true;
    openApp(world);
    expectMode("dark");
    expect(
      screen.getByRole("combobox", { name: "Appearance" }).textContent,
    ).toContain("System");
  });

  it.each(["", "sepia", "DARK", '"dark"'])(
    "ignores invalid stored preference %j",
    (value) => {
      window.localStorage.setItem(storageKey, value);
      dark = true;
      openApp(world);
      expectMode("dark");
      expect(
        screen.getByRole("combobox", { name: "Appearance" }).textContent,
      ).toContain("System");
    },
  );

  it("tolerates an inaccessible storage getter and still switches appearance", async () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("Storage denied", "SecurityError");
    });
    dark = true;
    openApp(world);
    expectMode("dark");
    await select("Light");
    expectMode("light");
    await select("System");
    expectMode("dark");
  });

  it("tolerates read failures and quota failures", async () => {
    vi.spyOn(window, "localStorage", "get").mockReturnValue({
      getItem: () => {
        throw new Error("Read denied");
      },
      setItem: () => {
        throw new DOMException("Full", "QuotaExceededError");
      },
    } as unknown as Storage);
    openApp(world);
    expectMode("light");
    await select("Dark");
    expectMode("dark");
    changeSystem(false);
    expectMode("dark");
  });

  it("works without storage or matchMedia", async () => {
    vi.stubGlobal("localStorage", undefined);
    vi.stubGlobal("matchMedia", undefined);
    openApp(world);
    expectMode("light");
    await select("Dark");
    expectMode("dark");
  });

  it("is keyboard accessible and preserves an unsaved campaign name", async () => {
    const { user } = openApp(world);
    const input = await screen.findByRole("textbox", { name: "Campaign name" });
    await user.type(input, "The Moonlit Road");
    const control = screen.getByRole("combobox", { name: "Appearance" });
    control.focus();
    await user.keyboard("d");
    expectMode("dark");
    expect(document.activeElement).toBe(control);
    expect((input as HTMLInputElement).value).toBe("The Moonlit Road");
    await user.keyboard("{ArrowDown}{Home}{Enter}");
    expectMode("light");
  });

  it.each([
    ["/", "Your campaigns"],
    ["/sheets", "Character sheets"],
    ["/join/accept", "Join a campaign"],
    ["/platform", "Platform accounts"],
    ["/platform/profile", "Your account"],
    ["/feedback", "Feedback inbox"],
    [`/campaigns/${ids.owned}/session`, "Session"],
    [`/campaigns/${ids.owned}/notes`, "The Sunless Citadel notes"],
    [`/campaigns/${ids.owned}/journal`, "The Sunless Citadel journal"],
    [`/campaigns/${ids.owned}/grants`, "Knowledge at your table"],
    [`/campaigns/${ids.owned}/entities/unknown`, "Knowledge"],
    ["/missing", "Page not found"],
  ])("offers appearance in the shared chrome at %s", async (path, title) => {
    window.localStorage.setItem(storageKey, "dark");
    openApp(world, path, worldOther);
    await screen.findByRole("heading", { level: 1, name: title });
    await waitFor(() =>
      expect(
        screen.getAllByRole("combobox", { name: "Appearance" }),
      ).toHaveLength(1),
    );
    expectMode("dark");
    await select("Light");
    expectMode("light");
  });
});
