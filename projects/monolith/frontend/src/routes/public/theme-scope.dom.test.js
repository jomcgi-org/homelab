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
  "../../lib/public/styles/technical-drawing.css",
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
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
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
  it("applies the public palette while its layout is mounted and drops it on navigation away", async () => {
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
  });

  it("renders the root error boundary as a technical-drawing sheet without the public marker", async () => {
    const target = mountInBody(ErrorBoundary);
    await tick();

    const sheet = target.querySelector("main.td.sheet");
    expect(sheet).not.toBeNull();
    expect(target.querySelector(".public-theme")).toBeNull();
    expect(document.body.matches(":has(.public-theme)")).toBe(false);
    expect(token(sheet, "--sheet")).toBe("#ffffff");
    expect(token(sheet, "--ink")).toBe("#26231c");
    // The design-system bridge resolves to the page's own tokens.
    expect(token(sheet, "--ds-surface")).toBe("#ffffff");
    expect(token(sheet, "--ds-ink")).toBe("#26231c");
    expect(token(document.documentElement, "--accent")).toBe("#0066ff");
  });

  it("keeps Grimoire and legacy Ember scoped while the Ember landing uses the sheet", async () => {
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
    const ember = emberTarget.querySelector(".td.sheet");
    expect(grimoire).not.toBeNull();
    expect(ember).not.toBeNull();
    // The public Grimoire pages sit on the sheet, so the ledger's roles
    // resolve to the sheet's tokens through the bridge in theme.css.
    expect(token(grimoire, "--grim-accent")).toBe(token(ember, "--accent-ink"));
    expect(token(grimoire, "--accent")).toBe(token(ember, "--accent-ink"));
    expect(token(ember, "--sheet")).toBe("#ffffff");
    expect(token(ember, "--ink")).toBe("#26231c");
    expect(emberTarget.querySelector(".ember-site")).toBeNull();
    const legacyEmber = document.createElement("div");
    legacyEmber.className = "ember-site";
    document.body.append(legacyEmber);
    expect(token(legacyEmber, "--em-ember")).toBe("#e0421a");
    expect(token(document.body, "--accent")).toBe("#ffde01");
  });
});
