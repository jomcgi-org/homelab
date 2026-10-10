// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  boundary,
  rolesFor,
  schemes,
} from "./technical-drawing.test-helper.js";

const require = createRequire(import.meta.url);
const contract = readFileSync(
  require.resolve("@homelab/design-system/tokens/contract.css"),
  "utf8",
);
const css = readFileSync(
  require.resolve("@homelab/design-system/tokens/technical-drawing.css"),
  "utf8",
);
const defaults = rolesFor(contract, ":root");
const themes = Object.fromEntries(
  schemes.map((scheme) => [scheme, rolesFor(css, boundary(scheme))]),
);
const themeOnly = Object.keys(themes.light).filter(
  (role) => !(role in defaults),
);

function normalize(value, roles = {}) {
  // happy-dom expands nested var() in direct values but not in the carrier.
  return value
    .replace(/var\((--ds-[a-z-]+)\)/g, (match, name) => roles[name] ?? match)
    .trim()
    .replace(/["']/g, "")
    .replace(/\s+/g, " ");
}

// happy-dom exposes only locally declared custom properties in getPropertyValue,
// but resolves inherited var() through computed longhands. font-family accepts
// arbitrary identifiers as a test carrier. No manual ancestor lookup is used.
function resolvedRole(element, role) {
  const previous = element.style.fontFamily;
  element.style.fontFamily = `var(${role}, missing-ds-role)`;
  const value = normalize(getComputedStyle(element).fontFamily);
  element.style.fontFamily = previous;
  return value;
}

function checkRoles(element, expected) {
  for (const [role, value] of Object.entries(expected)) {
    const direct = getComputedStyle(element).getPropertyValue(role);
    if (direct)
      expect(normalize(direct, expected), role).toBe(
        normalize(value, expected),
      );
    expect(normalize(resolvedRole(element, role), expected), role).toBe(
      normalize(value, expected),
    );
  }
}

function checkDefaults(element) {
  checkRoles(element, defaults);
  const computed = getComputedStyle(element);
  for (const role of themeOnly) {
    expect(computed.getPropertyValue(role)).toBe("");
    expect(resolvedRole(element, role), role).toBe("missing-ds-role");
  }
}

describe("technical-drawing boundary inheritance", () => {
  let style;
  beforeEach(() => {
    style = document.createElement("style");
    style.textContent = `${contract}\n${css}`;
    document.head.append(style);
  });
  afterEach(() => {
    style.remove();
    document.documentElement.removeAttribute("data-ds-theme");
    document.body.replaceChildren();
  });

  it.each(schemes)(
    "preserves the %s root boundary in either import order",
    (scheme) => {
      document.documentElement.dataset.dsTheme = `technical-drawing-${scheme}`;
      document.body.innerHTML = '<span id="root-child"></span>';
      for (const styles of [`${contract}\n${css}`, `${css}\n${contract}`]) {
        style.textContent = styles;
        checkRoles(document.documentElement, themes[scheme]);
        checkRoles(document.getElementById("root-child"), themes[scheme]);
        expect(getComputedStyle(document.documentElement).colorScheme).toBe(
          scheme,
        );
      }
    },
  );

  it("selects the declared light and dark roles explicitly", () => {
    document.body.innerHTML = schemes
      .map(
        (scheme) =>
          `<section id="${scheme}" data-ds-theme="technical-drawing-${scheme}"><span id="${scheme}-child"></span></section>`,
      )
      .join("");
    for (const scheme of schemes) {
      checkRoles(document.getElementById(scheme), themes[scheme]);
      checkRoles(document.getElementById(`${scheme}-child`), themes[scheme]);
      expect(
        getComputedStyle(document.getElementById(scheme)).colorScheme,
      ).toBe(scheme);
    }
  });

  it("uses the nearest boundary in light > dark > light and dark > light", () => {
    document.body.innerHTML = `
      <section id="outer-light" data-ds-theme="technical-drawing-light">
        <span id="outer-light-child"></span>
        <section id="inner-dark" data-ds-theme="technical-drawing-dark">
          <span id="inner-dark-child"></span>
          <section id="inner-light" data-ds-theme="technical-drawing-light">
            <span id="inner-light-child"></span>
          </section>
          <span id="after-inner-light"></span>
        </section>
        <span id="after-inner-dark"></span>
      </section>
      <span id="after-outer-light"></span>
      <section id="outer-dark" data-ds-theme="technical-drawing-dark">
        <section id="dark-inner-light" data-ds-theme="technical-drawing-light">
          <span id="dark-inner-light-child"></span>
        </section>
        <span id="after-dark-inner-light"></span>
      </section>
      <span id="after-outer-dark"></span>`;
    const expected = {
      "outer-light": "light",
      "outer-light-child": "light",
      "inner-dark": "dark",
      "inner-dark-child": "dark",
      "inner-light": "light",
      "inner-light-child": "light",
      "after-inner-light": "dark",
      "after-inner-dark": "light",
      "outer-dark": "dark",
      "dark-inner-light": "light",
      "dark-inner-light-child": "light",
      "after-dark-inner-light": "dark",
    };
    for (const [id, scheme] of Object.entries(expected)) {
      checkRoles(document.getElementById(id), themes[scheme]);
    }
    checkDefaults(document.getElementById("after-outer-light"));
    checkDefaults(document.getElementById("after-outer-dark"));
  });

  it("leaves every default intact and theme-only roles empty outside boundaries", () => {
    document.body.innerHTML = `
      <div id="unmarked"></div>
      <section data-ds-theme="technical-drawing-dark"><span></span></section>
      <div id="sibling"><span id="sibling-child"></span></div>
      <div id="unknown" data-ds-theme="system"></div>`;
    for (const id of ["unmarked", "sibling", "sibling-child", "unknown"]) {
      checkDefaults(document.getElementById(id));
    }
    checkDefaults(document.body);
    checkDefaults(document.documentElement);
    // Loading in the opposite order must not change explicit or default roles.
    style.textContent = `${css}\n${contract}`;
    checkDefaults(document.getElementById("sibling"));
    checkRoles(document.querySelector("section"), themes.dark);
  });
});
