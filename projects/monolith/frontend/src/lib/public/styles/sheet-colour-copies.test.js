import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// The sheet colours live in technical-drawing.css. Four places need them
// before or outside any stylesheet (the theme-color metas, the before-paint
// scheme bootstrap, the web app manifest), so they carry copies. This keeps
// the copies equal to the tokens: change --sheet and this names every copy.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

const css = read("./technical-drawing.css");
const dayBlock = css.match(/\.td-chrome,\s*\.td\s*\{([^}]*)\}/)[1];
const nightBlock = css.match(
  /:root\[data-theme="dark"\] \.td-chrome,\s*:root\[data-theme="dark"\] \.td\s*\{([^}]*)\}/,
)[1];
const sheet = (block) => block.match(/--sheet:\s*(#[0-9a-f]{6})/i)[1];
const day = sheet(dayBlock);
const night = sheet(nightBlock);

describe("sheet colour copies", () => {
  it("reads both schemes from the tokens", () => {
    expect(day).toMatch(/^#[0-9a-f]{6}$/i);
    expect(night).toMatch(/^#[0-9a-f]{6}$/i);
    expect(day).not.toBe(night);
  });

  it("app.html paints the browser chrome in each scheme's sheet", () => {
    const html = read("../../../app.html");
    expect(html).toMatch(
      new RegExp(
        `media="\\(prefers-color-scheme: light\\)"\\s+content="${day}"`,
      ),
    );
    expect(html).toMatch(
      new RegExp(
        `media="\\(prefers-color-scheme: dark\\)"\\s+content="${night}"`,
      ),
    );
  });

  it("the before-paint bootstrap writes the same two colours", () => {
    const bootstrap = read("../components/SchemePreference.svelte");
    expect(bootstrap).toContain(`t === "dark" ? "${night}" : "${day}"`);
  });

  it("the manifest uses the day sheet", () => {
    const manifest = read("../../manifest.js");
    expect(manifest).toContain(`background_color: "${day}"`);
    expect(manifest).toContain(`theme_color: "${day}"`);
  });
});
