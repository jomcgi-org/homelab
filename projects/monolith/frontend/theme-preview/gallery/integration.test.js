import { expect, it } from "vitest";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import config from "../vite.config.js";
import { renderFixture as renderTheme } from "../server-render.js";
import { renderFixture as renderGallery } from "./server-render.js";

const plugin = config.plugins.find(
  ({ name }) => name === "synthetic-theme-ssr",
);

it("keeps the original page SSR unchanged and renders the gallery only at its own entry", async () => {
  expect(config.build.rollupOptions.input).toEqual({
    index: resolve("theme-preview/index.html"),
    gallery: resolve("theme-preview/gallery.html"),
  });
  for (const [filename, render] of [
    ["index.html", renderTheme],
    ["gallery.html", renderGallery],
  ]) {
    const path = resolve("theme-preview", filename);
    const html = await readFile(path, "utf8");
    const expected = await render();
    const actual = await plugin.transformIndexHtml.handler(html, {
      filename: path,
    });
    expect(actual).toBe(
      html
        .replace("<!--fixture-ssr-->", expected.body)
        .replace("</head>", `${expected.head}</head>`),
    );
    expect(actual).not.toContain("<!--fixture-ssr-->");
    expect(actual.includes('data-gallery-section="dashboard"')).toBe(
      filename === "gallery.html",
    );
  }
});

it.each(["/projects/monolith/frontend/src/page.svelte", "$app/navigation"])(
  "rejects production module %s in the shared bundle guard",
  (id) => {
    expect(() =>
      plugin.generateBundle.call({
        getModuleIds: () => [id],
        error: (message) => {
          throw new Error(message);
        },
      }),
    ).toThrow("Production module entered theme fixture");
  },
);

it("keeps gallery sources independent of wall clock, random inputs and motion", async () => {
  for (const filename of [
    "GalleryFixture.svelte",
    "fixtures.js",
    "main.js",
    "server.js",
  ]) {
    const source = await readFile(
      resolve("theme-preview/gallery", filename),
      "utf8",
    );
    expect(source).not.toMatch(
      /Date\.now\s*\(|new Date\s*\(\s*\)|Math\.random\s*\(|performance\.now\s*\(/,
    );
    expect(source).not.toMatch(/\b(?:animation|transition)\s*:/);
    expect(source).not.toMatch(/\bfetch\s*\(/);
  }
});
