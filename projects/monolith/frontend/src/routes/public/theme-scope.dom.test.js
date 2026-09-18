// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRawSnippet, mount, tick, unmount } from "svelte";
import ErrorBoundary from "../+error.svelte";
import GrimoireShare from "./app/grimoire/chat/s/[id]/+page@.svelte";
import EmberPage from "./ember/+page.svelte";
import PublicLayout from "./+layout.svelte";

const styles = [
  "../../lib/styles/shared/tokens.css",
  "../../lib/public/styles/design-system.css",
  "../../lib/public/ember/ember.css",
  "../../lib/grimoire/theme.css",
]
  .map((path) => readFileSync(new URL(path, import.meta.url), "utf8"))
  .join("\n");

const children = createRawSnippet(() => ({
  render: () => "<main>public route</main>",
}));

const mounted = [];

function mountInBody(component, props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const instance = mount(component, { target, props });
  mounted.push({ instance, target });
  return target;
}

function token(element, name) {
  return getComputedStyle(element).getPropertyValue(name).trim();
}

beforeEach(() => {
  const style = document.createElement("style");
  style.textContent = styles;
  document.head.append(style);
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
});

afterEach(async () => {
  for (const { instance, target } of mounted.splice(0).reverse()) {
    await unmount(instance);
    target.remove();
  }
  document.head.innerHTML = "";
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("rendered theme scopes", () => {
  it(
    "applies the public palette while its layout is mounted and drops it on navigation away",
    async () => {
      const target = mountInBody(PublicLayout, {
        data: { maintenanceBanner: null },
        children,
      });
      await tick();

      expect(target.querySelector(".public-theme")).not.toBeNull();
      expect(document.body.matches(":has(.public-theme)")).toBe(true);
      expect(token(document.body, "--accent")).toBe("#ffde01");
      expect(token(document.body, "--cream")).toBe("#f3ede1");

      const [{ instance }] = mounted.splice(0);
      await unmount(instance);
      target.remove();
      await tick();

      expect(document.body.matches(":has(.public-theme)")).toBe(false);
      expect(token(document.documentElement, "--accent")).toBe("#0066ff");
      expect(token(document.documentElement, "--cream")).toBe("#f1ebdc");
    },
  );

  it("renders the root error boundary with the brutalist palette", async () => {
    const target = mountInBody(ErrorBoundary);
    await tick();

    expect(target.querySelector(".nf")).not.toBeNull();
    expect(target.querySelector(".public-theme")).not.toBeNull();
    expect(token(document.body, "--accent")).toBe("#ffde01");
    expect(token(document.body, "--coral")).toBe("#ff7169");
  });

  it(
    "keeps rendered Grimoire and Ember surfaces distinct inside the public scope",
    async () => {
      mountInBody(PublicLayout, {
        data: { maintenanceBanner: null },
        children,
      });
      const grimoireTarget = mountInBody(GrimoireShare, {
        data: { messages: [], turnstileSiteKey: "" },
      });
      const emberTarget = mountInBody(EmberPage, {
        data: { status: null, savings: null },
      });
      await tick();

      const grimoire = grimoireTarget.querySelector(".grimoire");
      const ember = emberTarget.querySelector(".ember-site");
      expect(grimoire).not.toBeNull();
      expect(ember).not.toBeNull();
      expect(token(grimoire, "--grim-accent")).toBe("#33507a");
      expect(token(grimoire, "--accent")).toBe("#33507a");
      expect(token(ember, "--em-ember")).toBe("#e0421a");
      expect(token(document.body, "--accent")).toBe("#ffde01");
    },
  );
});
